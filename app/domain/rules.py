"""Deterministic billing checks. See ADR-003.

The model never does what a reliable ``if`` can do. Everything computable from
dates, amounts, statuses and identifiers stays here, in code that is cheap,
instant, testable and identical on every run.

Each rule returns a :class:`RuleFinding` or ``None``. A rule states a fact and
shows its evidence; it never recommends an action.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.bank import (
    BankCheck,
    BankCredit,
    BankRead,
    BankReturn,
    CheckedCredit,
    CheckedReturn,
    CheckReason,
    Limitation,
    NotEvaluableReason,
)
from app.domain.enums import (
    InvoiceStatus,
    PaymentStatus,
    RuleCode,
    Severity,
    SubscriptionStatus,
)
from app.domain.injection import suspected_prompt_injection
from app.domain.models import (
    Customer,
    Invoice,
    InvoiceOwner,
    Payment,
    RuleFinding,
    Subscription,
)
from app.domain.references import cited_invoices

__all__ = [
    "bank_credit_check",
    "checks_not_evaluable",
    "cited_invoice_conflict",
    "identity_conflict",
    "currency_mismatch",
    "detect_duplicate_invoice",
    "duplicate_payment",
    "invoice_after_cancellation",
    "proration_mismatch",
    "suspected_impersonation",
    "suspected_prompt_injection",
    "suspicious_usage_delta",
    "unknown_customer",
    "unknown_invoice",
    "is_bank_finding",
]

#: Relative gap beyond which billed usage stops matching expected usage.
DEFAULT_USAGE_TOLERANCE = Decimal("0.25")

#: Proration is allowed to differ by this many minor units, to absorb the
#: provider's own rounding without hiding a real mismatch.
DEFAULT_PRORATION_TOLERANCE_MINOR = 1

#: Public mail providers. Anybody can hold an address there, so sharing one
#: with a customer says nothing about who the sender is.
WEBMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "bbox.fr",
        "free.fr",
        "gmail.com",
        "gmx.com",
        "gmx.fr",
        "googlemail.com",
        "hotmail.com",
        "hotmail.fr",
        "icloud.com",
        "laposte.net",
        "live.com",
        "live.fr",
        "me.com",
        "msn.com",
        "orange.fr",
        "outlook.com",
        "outlook.fr",
        "proton.me",
        "protonmail.com",
        "sfr.fr",
        "wanadoo.fr",
        "yahoo.com",
        "yahoo.fr",
    }
)

#: Legal forms dropped from a company name before looking for it in a message.
LEGAL_FORMS = frozenset(
    {"eurl", "gmbh", "inc", "llc", "ltd", "sa", "sarl", "sas", "sasu", "sci", "snc"}
)

#: Shortest company name, once normalised, still worth looking for.
MIN_COMPANY_NAME_LENGTH = 3

#: A sender domain starting with a customer's name (acme-fr.com for acme.fr)
#: is a lookalike only when the name is at least this long.
MIN_LOOKALIKE_PREFIX = 4

#: One letter off counts as a lookalike only on names at least this long:
#: on a short name, one letter off is another word (acne, acme).
MIN_LOOKALIKE_EDIT = 6

#: Characters that read alike in a domain name, folded before comparing.
_LOOKALIKE_FOLDS = (("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"))


def detect_duplicate_invoice(target: Invoice, recent: list[Invoice]) -> RuleFinding | None:
    """Two live invoices charging the same subscription for the same period.

    Equal totals and overlapping periods on one subscription is the signature
    of a double charge. Voided invoices are ignored: a cancelled duplicate
    never took the customer's money.

    When either invoice carries tax, the charge is compared as agreed --
    excluding tax -- and by rate: equal amounts excluding tax and the same tax
    at each rate (specs/013-invoice-tax, FR-005). Two untaxed invoices are
    compared as before.
    """
    if target.status is InvoiceStatus.VOID or target.subscription_id is None:
        return None

    for other in recent:
        if other.id == target.id:
            continue
        if other.status is InvoiceStatus.VOID:
            continue
        if other.subscription_id != target.subscription_id:
            continue
        if other.total != target.total:
            continue
        # The rates first: they differ whenever only one invoice is taxed, and
        # an untaxed invoice's lines are never summed (they may be in several
        # currencies, which `currency_mismatch` reports).
        if (target.taxed or other.taxed) and (
            other.tax_by_rate != target.tax_by_rate
            or other.amount_excluding_tax != target.amount_excluding_tax
        ):
            continue
        if not target.overlaps_period_of(other):
            continue

        return RuleFinding(
            code=RuleCode.DUPLICATE_INVOICE,
            severity=Severity.HIGH,
            description=(
                "Deux factures couvrent la même période d'abonnement pour un montant identique."
            ),
            evidence={
                "invoice_1": other.id,
                "invoice_2": target.id,
                "subscription_id": target.subscription_id,
                "amount_minor": target.total.amount_minor,
                "currency": target.total.currency,
                "period_start": _iso(target.period_start),
                "period_end": _iso(target.period_end),
            },
        )
    return None


def currency_mismatch(
    target: Invoice, invoices: list[Invoice], payments: list[Payment]
) -> RuleFinding | None:
    """Amounts the rules must compare are in different currencies (taxonomy S10).

    No rule converts: an implicit exchange rate inside a dispute would invent
    money (ADR-005). Where a comparison is impossible, this says so instead
    of every other rule concluding in silence -- a duplicate charged once in
    euros and once in dollars is not "no duplicate". Several currencies on an
    account are not an anomaly; only the comparisons this invoice needs are
    checked: its own lines, the payments made against it, and the live
    invoices covering the same period of the same subscription.
    """
    if target.status is InvoiceStatus.VOID:
        return None
    currency = target.total.currency
    clashes: list[dict] = []
    for index, line in enumerate(target.lines):
        for amount in (line.unit_amount, line.amount):
            if amount.currency != currency:
                clashes.append({"line_index": index, "currency": amount.currency})
                break
    for payment in payments:
        if payment.invoice_id == target.id and payment.amount.currency != currency:
            clashes.append({"payment_id": payment.id, "currency": payment.amount.currency})
    for other in invoices:
        if (
            other.id != target.id
            and other.status is not InvoiceStatus.VOID
            and target.subscription_id is not None
            and other.subscription_id == target.subscription_id
            and other.total.currency != currency
            and target.overlaps_period_of(other)
        ):
            clashes.append({"invoice_id": other.id, "currency": other.total.currency})
    if not clashes:
        return None
    return RuleFinding(
        code=RuleCode.CURRENCY_MISMATCH,
        severity=Severity.MEDIUM,
        description=(
            "Des montants à comparer sont en devises différentes : "
            "aucune conclusion n'est tirée, une personne examine."
        ),
        evidence={"invoice_id": target.id, "currency": currency, "clashes": clashes},
    )


def checks_not_evaluable(
    target: Invoice, subscription: Subscription | None, invoices: list[Invoice]
) -> RuleFinding | None:
    """The checks that apply to this invoice but cannot conclude (D14).

    A rule returns None both when it found nothing and when it lacked what it
    needed, and the second must not read as the first. This states, once per
    invoice, which checks could not conclude and why. A check that does not
    apply is not listed: an invoice with no subscription -- a one-off charge --
    has nothing for the subscription checks to say. Usage has its own
    finding, ``USAGE_UNAVAILABLE``.
    """
    if target.status is InvoiceStatus.VOID or target.subscription_id is None:
        return None
    gaps: list[dict] = []

    # Duplicate: applies only when another live invoice on the subscription
    # has the same total; then both periods are needed, and must be real.
    candidates = [
        other
        for other in invoices
        if other.id != target.id
        and other.status is not InvoiceStatus.VOID
        and other.subscription_id == target.subscription_id
        and other.total == target.total
    ]
    if candidates and not _has_period(target):
        gaps.append({"check": "duplicate_invoice", "reason": "invoice_without_period"})
    elif any(not _has_period(other) for other in candidates):
        gaps.append({"check": "duplicate_invoice", "reason": "candidate_without_period"})

    # Cancellation and proration need the subscription itself.
    if subscription is None:
        gaps.append({"check": "billing_after_cancellation", "reason": "subscription_not_found"})
        gaps.append({"check": "proration", "reason": "subscription_not_found"})
    else:
        # A subscription marked cancelled with no date: when it ended cannot
        # be known. One never cancelled simply does not apply.
        undated = subscription.cancelled_at is None
        if subscription.status is SubscriptionStatus.CANCELLED and undated:
            gaps.append(
                {"check": "billing_after_cancellation", "reason": "cancellation_date_missing"}
            )
        if subscription.period_seconds <= 0:
            gaps.append({"check": "proration", "reason": "subscription_period_unknown"})
        elif any(line.period_seconds is None or line.period_seconds <= 0 for line in target.lines):
            gaps.append({"check": "proration", "reason": "line_without_valid_period"})
        else:
            inside = [line for line in target.lines if _in_current_period(line, subscription)]
            cycle = _one_cycle(subscription)
            if inside and cycle is False:
                # A first period started before its anchor: itself a part of
                # a cycle, so nothing inside it can be measured against it.
                gaps.append({"check": "proration", "reason": "current_period_not_one_cycle"})
            elif inside and cycle is None:
                # Without the cadence the current period may be such a part.
                gaps.append({"check": "proration", "reason": "billing_cycle_unknown"})
            if any(
                not _in_current_period(line, subscription)
                and not _whole_periods(line, subscription)
                for line in target.lines
            ):
                # A part of another period: whose length is not known here.
                gaps.append({"check": "proration", "reason": "line_outside_current_period"})

    if not gaps:
        return None
    return RuleFinding(
        code=RuleCode.NOT_EVALUABLE,
        severity=Severity.LOW,
        description=(
            "Des contrôles n'ont pas pu conclure faute de données : leur silence "
            "ne vaut pas absence d'anomalie."
        ),
        evidence={"invoice_id": target.id, "checks": gaps},
    )


def _in_current_period(line, sub: Subscription) -> bool:
    return (
        sub.current_period_start <= line.period_start and line.period_end <= sub.current_period_end
    )


def _whole_periods(line, sub: Subscription) -> bool:
    """Both bounds on the subscription's schedule, from an anchor set no later
    than the line starts: the line covers whole periods (arrears billed after
    a renewal, a period after a cancellation), and there is nothing to
    prorate. An anchor set later -- reset by a trial or a new interval --
    proves nothing about an older line; without a known cadence and anchor
    nothing is taken for whole periods."""
    return (
        line.period_end > line.period_start
        and sub.billing_anchor is not None
        and sub.billing_anchor <= line.period_start
        and _on_schedule(line.period_start, sub)
        and _on_schedule(line.period_end, sub)
    )


def _one_cycle(sub: Subscription) -> bool | None:
    """Whether the current period is exactly one step of the schedule. An
    anchor after its start proves it partial on its own; otherwise None when
    the cadence is unknown -- and nothing is measured against it."""
    start, end = sub.current_period_start, sub.current_period_end
    if sub.billing_anchor is not None and sub.billing_anchor > start:
        return False
    if not _cadence_known(sub):
        return None
    return (
        sub.billing_anchor <= start
        and _on_schedule(start, sub)
        and _on_schedule(end, sub)
        and end.astimezone(UTC) == _next_step(start, sub)
    )


def exactly_one_cycle(sub: Subscription) -> bool:
    """Whether the subscription's current period is exactly one step of its
    schedule, by the calendar the checks use -- for a platform's mapper that
    must refuse a period the checks could not measure (specs/014)."""
    return _one_cycle(sub) is True


def _cadence_known(sub: Subscription) -> bool:
    count = sub.billing_interval_count
    return (
        sub.billing_anchor is not None
        and sub.billing_interval in ("day", "week", "month", "year")
        and count is not None
        and count >= 1
    )


def _next_step(moment: datetime, sub: Subscription) -> datetime:
    """The schedule's step after `moment`, which is on it."""
    anchor, unit, count = sub.billing_anchor, sub.billing_interval, sub.billing_interval_count
    moment, anchor = moment.astimezone(UTC), anchor.astimezone(UTC)
    if unit in ("day", "week"):
        return moment + timedelta(days=count * (7 if unit == "week" else 1))
    months = count * (12 if unit == "year" else 1)
    elapsed = (moment.year - anchor.year) * 12 + moment.month - anchor.month
    return _months_after(anchor, elapsed + months)


def _on_schedule(moment: datetime, sub: Subscription) -> bool:
    """`moment` is the anchor stepped a whole number of times by the cadence:
    days and weeks exactly, months and years on the anchor's own day, clamped
    to a short month's last day -- the 31st steps to February 28, and
    January 28 is no step of it."""
    if not _cadence_known(sub):
        return False
    anchor, unit, count = sub.billing_anchor, sub.billing_interval, sub.billing_interval_count
    moment, anchor = moment.astimezone(UTC), anchor.astimezone(UTC)
    if unit in ("day", "week"):
        step = timedelta(days=count * (7 if unit == "week" else 1))
        return (moment - anchor) % step == timedelta(0)
    if unit in ("month", "year"):
        months = count * (12 if unit == "year" else 1)
        elapsed = (moment.year - anchor.year) * 12 + moment.month - anchor.month
        return elapsed % months == 0 and moment == _months_after(anchor, elapsed)
    return False


def _months_after(anchor: datetime, count: int) -> datetime:
    months = anchor.year * 12 + anchor.month - 1 + count
    year, month = divmod(months, 12)
    day = min(anchor.day, calendar.monthrange(year, month + 1)[1])
    return anchor.replace(year=year, month=month + 1, day=day)


def _has_period(invoice: Invoice) -> bool:
    """A period that exists and runs forwards, on every line."""
    return bool(invoice.lines) and all(
        line.period_seconds is not None and line.period_seconds > 0 for line in invoice.lines
    )


def duplicate_payment(target: Invoice, payments: list[Payment]) -> RuleFinding | None:
    """One invoice collected twice: two distinct settled payments, each its
    full total. The payment-side face of a duplicate; the correction is a
    refund of the extra payment, never a credit note on the invoice."""
    if target.status is InvoiceStatus.VOID or target.total.amount_minor <= 0:
        return None
    full = sorted(
        {
            p.id
            for p in payments
            if p.invoice_id == target.id
            and p.status is PaymentStatus.SUCCEEDED
            and p.amount == target.total
        }
    )
    if len(full) < 2:
        return None
    return RuleFinding(
        code=RuleCode.DUPLICATE_PAYMENT,
        severity=Severity.HIGH,
        description="La même facture a été encaissée deux fois, pour son montant total.",
        evidence={
            "invoice_id": target.id,
            "payment_ids": full,
            "amount_minor": target.total.amount_minor,
            "currency": target.total.currency,
        },
    )


def cited_invoice_conflict(cited: Iterable[str], designated: str | None) -> RuleFinding | None:
    """The message cites invoice references, none of them the designated one
    (taxonomy S6).

    Only a conflict is flagged. A message that cites the designated invoice
    and others -- a comparison, a history, a suspected duplicate -- is
    ordinary. `cited` and `designated` come from the provider, which alone
    knows how its references are written and normalised; a reference written
    freely ("my September invoice") is not detected.
    """
    cited = sorted(set(cited))
    if not designated or not cited or designated in cited:
        return None
    return RuleFinding(
        code=RuleCode.INVOICE_REFERENCE_CONFLICT,
        severity=Severity.MEDIUM,
        description=(
            "Le message cite d'autres factures que celle désignée : "
            "demander laquelle est contestée."
        ),
        evidence={"designated": designated, "cited": cited},
    )


def invoice_after_cancellation(invoice: Invoice, sub: Subscription) -> RuleFinding | None:
    """A subscription was billed for a period starting after its cancellation."""
    if sub.cancelled_at is None or invoice.status is InvoiceStatus.VOID:
        return None

    covered_from = invoice.period_start or invoice.issued_at
    if covered_from <= sub.cancelled_at:
        return None

    return RuleFinding(
        code=RuleCode.INVOICE_AFTER_CANCELLATION,
        severity=Severity.HIGH,
        description="Une facture couvre une période postérieure à la résiliation.",
        evidence={
            "invoice_id": invoice.id,
            "subscription_id": sub.id,
            "cancelled_at": _iso(sub.cancelled_at),
            "covered_from": _iso(covered_from),
            "amount_minor": invoice.total.amount_minor,
            "currency": invoice.total.currency,
        },
    )


def suspicious_usage_delta(
    expected: Decimal,
    billed: Decimal,
    tolerance: Decimal = DEFAULT_USAGE_TOLERANCE,
) -> RuleFinding | None:
    """Billed usage diverges from expected usage beyond `tolerance`.

    Quantities are ``Decimal``, not ``Money``: this compares consumption, not
    amounts.
    """
    if expected <= 0:
        if billed <= 0:
            return None
        relative = None
    else:
        relative = abs(billed - expected) / expected
        if relative <= tolerance:
            return None

    return RuleFinding(
        code=RuleCode.SUSPICIOUS_USAGE_DELTA,
        severity=Severity.MEDIUM,
        description="L'usage facturé s'écarte nettement de l'usage attendu.",
        evidence={
            "expected": str(expected),
            "billed": str(billed),
            "relative_delta": str(relative) if relative is not None else None,
            "tolerance": str(tolerance),
            # Over-billing is the customer's problem; under-billing is ours,
            # and is never put to the customer (taxonomy S1).
            "direction": "over" if billed > expected else "under",
        },
    )


def proration_mismatch(
    invoice: Invoice,
    sub: Subscription,
    tolerance_minor: int = DEFAULT_PRORATION_TOLERANCE_MINOR,
) -> RuleFinding | None:
    """A partial-period line is not priced pro rata of its own period.

    Only lines covering *less* than the subscription period are checked: those
    are the ones a plan change mid-month produces, and the ones customers
    dispute.

    The ratio is the line's duration over the period's, in seconds, because
    providers prorate to the second from the moment of the change. Amounts are
    compared without their sign: a plan change is billed as a negative credit
    for the unused time on the old plan and a charge for the remaining time on
    the new one.

    Assumption: each line carries the full price of its own plan as its unit
    amount, as Stripe's proration lines do. A single net line (new plan minus
    old) carries no plan price, cannot be checked, and stays flagged.
    """
    if invoice.status is InvoiceStatus.VOID or sub.period_seconds <= 0:
        return None
    currency = invoice.total.currency
    if any(
        line.unit_amount.currency != currency or line.amount.currency != currency
        for line in invoice.lines
    ):
        return None  # not comparable; `currency_mismatch` says so
    if _one_cycle(sub) is not True:
        # A partial or unknown cycle: measuring against it would guess, and
        # `checks_not_evaluable` says so.
        return None
    for index, line in enumerate(invoice.lines):
        line_seconds = line.period_seconds
        if line_seconds is None or line_seconds <= 0 or line_seconds >= sub.period_seconds:
            continue
        # Only a line inside the current period is measured against it. One
        # outside covers whole periods (nothing to prorate) or part of another
        # period whose length is not known here: `checks_not_evaluable` says so.
        if not _in_current_period(line, sub):
            continue

        ratio = Decimal(line_seconds) / Decimal(sub.period_seconds)
        expected = abs(line.unit_amount.multiply(line.quantity).multiply(ratio))
        gap = abs(expected - abs(line.amount))
        if gap.amount_minor <= tolerance_minor:
            continue

        return RuleFinding(
            code=RuleCode.PRORATION_MISMATCH,
            severity=Severity.MEDIUM,
            description=(
                "Le montant proratisé ne correspond pas à la période couverte, en "
                "supposant que chaque ligne porte le prix plein de son forfait."
            ),
            evidence={
                # Stated rather than implied (taxonomy S9): a provider billing
                # one net line breaks it, and nothing here can tell.
                "assumption": "each line carries the full price of its own plan",
                "invoice_id": invoice.id,
                "line_index": index,
                "line_description": line.description,
                "line_seconds": line_seconds,
                "period_seconds": sub.period_seconds,
                "ratio": str(ratio),
                "expected_minor": expected.amount_minor,
                "billed_minor": line.amount.amount_minor,
                "currency": line.amount.currency,
            },
        )
    return None


def unknown_invoice(invoice_id: str, invoices: list[Invoice]) -> RuleFinding | None:
    """The disputed invoice reference matches nothing we hold for this customer."""
    if any(invoice.id == invoice_id for invoice in invoices):
        return None

    return RuleFinding(
        code=RuleCode.UNKNOWN_INVOICE,
        severity=Severity.MEDIUM,
        description="La facture contestée est introuvable pour ce client.",
        evidence={
            "invoice_id": invoice_id,
            "known_invoice_ids": [invoice.id for invoice in invoices],
        },
    )


def identity_conflict(
    claimed_customer_id: str | None, sender: Customer | None
) -> RuleFinding | None:
    """The request names a customer the sender's address does not resolve to (D9).

    The address identifies the sender; a `customer_id` is only a claim. When
    the address resolves to another customer, to nobody, or the claimed id
    does not exist, the claim is not believed. The claimed account is never
    looked up here -- not even to check that it exists -- so the answer is the
    same either way, and nothing about it leaks.
    """
    if claimed_customer_id is None:
        return None
    if sender is not None and sender.id == claimed_customer_id:
        return None
    return RuleFinding(
        code=RuleCode.IDENTITY_CONFLICT,
        severity=Severity.HIGH,
        description=(
            "L'adresse de l'expéditeur ne correspond pas au compte indiqué : une personne vérifie."
        ),
        evidence={
            "claimed_customer_id": claimed_customer_id,
            "sender_is_a_customer": sender is not None,
        },
    )


def unknown_customer(email: str | None, customer: Customer | None) -> RuleFinding | None:
    """The sender matches no customer we hold.

    A fact about the sender, not an accusation: most unknown addresses are a
    colleague or a personal mailbox. Whether the message reaches for someone
    else's account is :func:`suspected_impersonation`'s question.
    """
    if customer is not None:
        return None

    return RuleFinding(
        code=RuleCode.UNKNOWN_CUSTOMER,
        severity=Severity.LOW,
        description="L'adresse de l'expéditeur ne correspond à aucun client connu.",
        evidence={"sender_email": email},
    )


def suspected_impersonation(
    email: str | None,
    message: str,
    cited: InvoiceOwner | None,
    customers: list[Customer],
) -> RuleFinding | None:
    """An unknown sender reaches for a known customer's account. See ADR-007.

    Only meaningful once the sender has matched no customer. Any one signal is
    enough, and every signal found is reported:

    - ``known_invoice``: the dispute cites an invoice we hold -- `cited`,
      whose it is and nothing else of it;
    - ``same_domain``: the sender writes from a customer's domain, or a
      subdomain of it. Most likely a colleague -- which a human confirms with
      the known contact before anything is said about the account;
    - ``lookalike_domain``: the sender's domain imitates a customer's;
    - ``company_name``: the message names a customer.

    Webmail domains carry no identity and are never compared.
    """
    sender_domain = _domain(email)
    signals: list[dict] = []

    if cited is not None:
        signals.append(
            {
                "signal": "known_invoice",
                "customer_id": cited.customer_id,
                "invoice_id": cited.invoice_id,
            }
        )

    text = _normalise(message)
    for customer in customers:
        domain = _domain(customer.email)
        if _comparable(sender_domain) and _comparable(domain):
            if sender_domain == domain or sender_domain.endswith("." + domain):
                signals.append(
                    {"signal": "same_domain", "customer_id": customer.id, "domain": domain}
                )
            elif _looks_like(sender_domain, domain):
                signals.append(
                    {"signal": "lookalike_domain", "customer_id": customer.id, "domain": domain}
                )

        name = _company_name(customer.display_name)
        if name is not None and re.search(rf"\b{re.escape(name)}\b", text):
            signals.append(
                {
                    "signal": "company_name",
                    "customer_id": customer.id,
                    "name": customer.display_name,
                }
            )

    if not signals:
        return None

    return RuleFinding(
        code=RuleCode.SUSPECTED_IMPERSONATION,
        severity=Severity.HIGH,
        description="Un expéditeur inconnu vise le compte d'un client connu.",
        evidence={"sender_domain": sender_domain, "signals": signals},
    )


def _domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def _comparable(domain: str | None) -> bool:
    return domain is not None and domain not in WEBMAIL_DOMAINS


def _looks_like(sender_domain: str, customer_domain: str) -> bool:
    """Compare the names the domains are registered under, folded.

    Equal once folded (acme.com, acrne.fr), starting with the customer's name
    (acme-fr.com), or one letter off a long enough name (transport-nord.fr).
    """
    sender, customer = _skeleton(sender_domain), _skeleton(customer_domain)
    if sender == customer:
        return True
    if len(customer) >= MIN_LOOKALIKE_PREFIX and sender.startswith(customer):
        return True
    return len(customer) >= MIN_LOOKALIKE_EDIT and _edit_distance(sender, customer) <= 1


def _skeleton(domain: str) -> str:
    """The registered name, without extension, separators or lookalike letters."""
    labels = domain.split(".")
    name = labels[-2] if len(labels) >= 2 else labels[0]
    for lookalike, letter in _LOOKALIKE_FOLDS:
        name = name.replace(lookalike, letter)
    return name.replace("-", "")


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _normalise(text: str) -> str:
    """Lower case, no accents, words separated by single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stripped.casefold()).split())


def _company_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    words = [w for w in _normalise(display_name).split() if w not in LEGAL_FORMS]
    name = " ".join(words)
    return name if len(name) >= MIN_COMPANY_NAME_LENGTH else None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# -- the merchant's bank (specs/016-bank-evidence) ---------------------------------

#: How far before the window opens the bank is read, by creation instant, so that a
#: credit created a little before the invoice's issue and completed after it is read
#: (clarified 2026-11-14; a convention until the bank states a maximum delay).
BANK_MARGIN_DAYS = 7

#: The credit types that can be an outside payer's transfer, until the bank says which
#: type an incoming transfer carries (clarified 2026-11-14). Each still needs an external
#: origin to qualify: the rule decides that, not the mapper. A credit of any other type
#: is not read as a payment at all.
BANK_CREDIT_TYPES = frozenset({"topup", "transfer"})

#: The two findings the bank check makes; with a bank-derived `NOT_EVALUABLE`, each
#: blocks every correction and sends the case to a person (FR-004).
BANK_FINDINGS = frozenset({RuleCode.BANK_CREDIT_OBSERVED, RuleCode.BANK_CREDIT_TO_RECONCILE})

#: Said every time the check runs: what a reference-based observation cannot show.
BANK_LIMITATIONS: tuple[Limitation, ...] = (
    "allocation_unverified_beyond_loaded_invoices",
    "coverage_declared_accounts_from_issue",
    "later_reversal_not_followed",
)

#: One fixed text per code: a bank finding's description names no bank value, so the
#: explanations built from it -- and what an engine is shown -- carry none (FR-005).
BANK_DESCRIPTIONS: dict[RuleCode, str] = {
    RuleCode.BANK_CREDIT_OBSERVED: (
        "Un crédit bancaire cite cette facture : candidat fondé sur la référence, payeur "
        "non vérifié, rien n'est conclu sur la facture ; une personne examine."
    ),
    RuleCode.BANK_CREDIT_TO_RECONCILE: (
        "Un crédit bancaire cite cette facture sans la régler simplement : une personne "
        "rapproche, rien n'est conclu sur la facture."
    ),
    RuleCode.NOT_EVALUABLE: (
        "Le contrôle bancaire n'a pas pu conclure : son silence ne vaut ni paiement ni "
        "absence de paiement."
    ),
}

#: A credit that qualifies has exactly these reasons, and nothing else.
_QUALIFYING: tuple[CheckReason, ...] = ("reference_matched", "amount_equal")


def is_bank_finding(finding: RuleFinding) -> bool:
    """Either bank finding, or a `NOT_EVALUABLE` the bank check made."""
    return finding.code in BANK_FINDINGS or (
        finding.code is RuleCode.NOT_EVALUABLE and finding.evidence.get("check") == "bank"
    )


def bank_not_evaluable(invoice_id: str, reason: NotEvaluableReason) -> RuleFinding:
    """The bank check could not conclude on `invoice_id` (D14): said, never "nothing"."""
    return RuleFinding(
        code=RuleCode.NOT_EVALUABLE,
        severity=Severity.LOW,
        description=BANK_DESCRIPTIONS[RuleCode.NOT_EVALUABLE],
        evidence={"check": "bank", "invoice_id": invoice_id, "reason": reason},
    )


def bank_credit_check(
    invoice: Invoice, read: BankRead, invoices: list[Invoice], now: datetime
) -> tuple[RuleFinding | None, BankCheck]:
    """What the merchant's bank says of money received for `invoice`, unpaid on the
    platform (specs/016-bank-evidence, FR-003): a finding or none, and the record kept
    whatever it found.

    A credit is taken into account when it is of an allowlisted type, completed or
    reverted, on a declared account, placed in the window -- its `completed_at` from the
    invoice's issue to the frozen instant `now`, bounds inclusive; a reverted one with
    none, by a `created_at` before the window's end -- and its reference cites this
    invoice among the customer's loaded ones (`cited_invoices`). It qualifies when it is
    completed, in the invoice's currency, for its exact total, from an external origin,
    with no FX and no fee, cites no other loaded invoice, and no return relates to it.

    In order of precedence: a read that cannot be relied on is *not evaluable*; any
    referenced credit that does not qualify, or several that do, is *to reconcile*;
    exactly one qualifying credit is *observed*, a reference-based candidate; nothing
    referenced is no finding -- never evidence that the invoice is unpaid.
    """
    start, end = invoice.issued_at, now
    currency = invoice.total.currency
    base = {
        "invoice_id": invoice.id,
        "window_start": start,
        "window_end": end,
        "margin_days": read.margin_days,
        "accounts": read.accounts,
        "complete": read.complete,
        "coverage_established": read.coverage_established,
        "limitations": BANK_LIMITATIONS,
    }

    unusable = _bank_read_unusable(read, currency, start, end)
    if unusable is not None:
        return bank_not_evaluable(invoice.id, unusable), BankCheck(
            outcome="not_evaluable", reason=unusable, **base
        )

    pool = invoices if any(i.id == invoice.id for i in invoices) else [*invoices, invoice]
    declared = {account.id for account in read.accounts}
    referenced: list[tuple[BankCredit, set[str]]] = []
    for credit in sorted(read.credits, key=_credit_order):
        if (
            credit.type not in BANK_CREDIT_TYPES
            or credit.state not in ("completed", "reverted")
            or credit.account_id not in declared
            or not _in_bank_window(credit, start, end)
        ):
            continue
        cited = cited_invoices(credit.reference, pool)
        if invoice.id in cited:
            referenced.append((credit, cited))

    if not referenced:
        return None, BankCheck(outcome="nothing", **base)

    items: list[CheckedCredit | CheckedReturn] = []
    recorded_returns: set[str] = set()
    qualifying: list[str] = []
    exceptions: set[CheckReason] = set()
    for credit, cited in referenced:
        returns = _returns_of(credit, read.returns, end)
        reasons = _credit_reasons(credit, cited, currency, invoice.total.amount_minor, returns)
        if reasons == _QUALIFYING and credit.state == "completed":
            qualifying.append(credit.transaction_id)
        else:
            exceptions.update(r for r in reasons if r not in _QUALIFYING)
        items.append(
            CheckedCredit(
                account_id=credit.account_id,
                transaction_id=credit.transaction_id,
                amount=credit.amount,
                completed_at=credit.completed_at,
                state=credit.state,
                observed_at=credit.observed_at,
                reasons=reasons,
            )
        )
        for item in returns:
            if item.id not in recorded_returns:
                recorded_returns.add(item.id)
                items.append(_checked_return(item))

    if exceptions or len(qualifying) > 1:
        several: set[CheckReason] = {"several_candidates"} if len(qualifying) > 1 else set()
        reasons = tuple(sorted(exceptions | several))
        code, outcome = RuleCode.BANK_CREDIT_TO_RECONCILE, "to_reconcile"
    else:
        reasons = ()
        code, outcome = RuleCode.BANK_CREDIT_OBSERVED, "observed"
    finding = RuleFinding(
        code=code,
        severity=Severity.MEDIUM,
        description=BANK_DESCRIPTIONS[code],
        evidence={
            "check": "bank",
            "invoice_id": invoice.id,
            "transaction_ids": [credit.transaction_id for credit, _ in referenced],
            "reasons": list(reasons),
        },
    )
    return finding, BankCheck(outcome=outcome, items=tuple(items), reasons=reasons, **base)


def _bank_read_unusable(
    read: BankRead, currency: str, start: datetime, end: datetime
) -> NotEvaluableReason | None:
    """Why the read cannot be stood on for an invoice in `currency`, or None."""
    if "account_not_visible" in read.incompleteness or any(
        not account.visible for account in read.accounts
    ):
        return "account_not_visible"
    # A read that did not ask for the whole window cannot answer for it either.
    if (
        not read.complete
        or read.incompleteness
        or read.window_start > start
        or read.window_end < end
    ):
        return "read_incomplete"
    if not any(account.currency == currency for account in read.accounts):
        return "no_account_in_currency"
    if not read.coverage_established:
        return "coverage_not_established"
    return None


def _in_bank_window(credit: BankCredit, start: datetime, end: datetime) -> bool:
    if credit.completed_at is not None:
        return start <= credit.completed_at <= end
    # A reverted credit may carry no completion instant: kept, never silently dropped,
    # when it was created before the window's end.
    return credit.state == "reverted" and credit.created_at <= end


def _credit_order(credit: BankCredit) -> tuple:
    return (credit.completed_at or credit.created_at, credit.transaction_id)


def _return_instant(item: BankReturn) -> datetime:
    return item.completed_at or item.created_at


def _returns_of(
    credit: BankCredit, returns: tuple[BankReturn, ...], end: datetime
) -> list[BankReturn]:
    """The returns that may give `credit`'s money back, up to the window's end: one that
    names it, and -- until the bank says how a reversal appears -- one that names no
    credit, in the credit's currency, for its amount, after its creation. Said, never
    weighed: either makes the credit one to reconcile."""
    related = []
    for item in returns:
        instant = _return_instant(item)
        if instant > end:
            continue
        if item.related_credit_id == credit.transaction_id or (
            item.related_credit_id is None
            and item.amount.currency == credit.amount.currency
            and abs(item.amount.amount_minor) == credit.amount.amount_minor
            and instant >= credit.created_at
        ):
            related.append(item)
    return sorted(related, key=lambda r: (_return_instant(r), r.id))


def _credit_reasons(
    credit: BankCredit,
    cited: set[str],
    currency: str,
    total_minor: int,
    returns: list[BankReturn],
) -> tuple[CheckReason, ...]:
    reasons: list[CheckReason] = ["reference_matched"]
    if credit.amount.currency != currency:
        reasons.append("currency_differs")
    elif credit.amount.amount_minor == total_minor:
        reasons.append("amount_equal")
    elif credit.amount.amount_minor < total_minor:
        reasons.append("amount_partial")
    else:
        reasons.append("amount_over")
    if credit.state == "reverted":
        reasons.append("reversed")
    if credit.fx:
        reasons.append("fx_present")
    if credit.fee:
        reasons.append("fee_present")
    if len(cited) > 1:
        reasons.append("several_invoices_cited")
    if credit.origin == "unknown":
        reasons.append("origin_unknown")
    elif credit.origin == "internal":
        reasons.append("origin_internal")
    if returns:
        reasons.append("related_return_found")
    return tuple(reasons)


def _checked_return(item: BankReturn) -> CheckedReturn:
    return CheckedReturn(
        id=item.id,
        type=item.type,
        amount=item.amount,
        related_credit_id=item.related_credit_id,
        instant=_return_instant(item),
        instant_is="completed" if item.completed_at is not None else "created",
    )

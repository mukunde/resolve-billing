"""Deterministic billing checks. See ADR-003.

The model never does what a reliable ``if`` can do. Everything computable from
dates, amounts, statuses and identifiers stays here, in code that is cheap,
instant, testable and identical on every run.

Each rule returns a :class:`RuleFinding` or ``None``. A rule states a fact and
shows its evidence; it never recommends an action.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal

from app.domain.enums import InvoiceStatus, PaymentStatus, RuleCode, Severity
from app.domain.injection import suspected_prompt_injection
from app.domain.models import Customer, Invoice, Payment, RuleFinding, Subscription

__all__ = [
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
        if not target.overlaps_period_of(other):
            continue

        return RuleFinding(
            code=RuleCode.DUPLICATE_INVOICE,
            severity=Severity.HIGH,
            description=(
                "Deux factures couvrent la même période d'abonnement "
                "pour un montant identique."
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


def cited_invoice_conflict(
    cited: Iterable[str], designated: str | None
) -> RuleFinding | None:
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
    for index, line in enumerate(invoice.lines):
        line_seconds = line.period_seconds
        if line_seconds is None or line_seconds <= 0 or line_seconds >= sub.period_seconds:
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
            "L'adresse de l'expéditeur ne correspond pas au compte indiqué : "
            "une personne vérifie."
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
    invoice: Invoice | None,
    customers: list[Customer],
) -> RuleFinding | None:
    """An unknown sender reaches for a known customer's account. See ADR-007.

    Only meaningful once the sender has matched no customer. Any one signal is
    enough, and every signal found is reported:

    - ``known_invoice``: the dispute cites an invoice we hold;
    - ``same_domain``: the sender writes from a customer's domain, or a
      subdomain of it. Most likely a colleague -- which a human confirms with
      the known contact before anything is said about the account;
    - ``lookalike_domain``: the sender's domain imitates a customer's;
    - ``company_name``: the message names a customer.

    Webmail domains carry no identity and are never compared.
    """
    sender_domain = _domain(email)
    signals: list[dict] = []

    if invoice is not None:
        signals.append(
            {
                "signal": "known_invoice",
                "customer_id": invoice.customer_id,
                "invoice_id": invoice.id,
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

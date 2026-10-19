"""ADR-003: every rule gets a case that fires and a case that must not.

The must-not cases matter more. An agent that finds a duplicate everywhere is
worse than no agent at all -- it refunds by reflex. Scenario B of the demo
exists for exactly this reason.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.application import investigate_dispute as investigation_module
from app.domain import automated as automated_module
from app.domain import injection as injection_module
from app.domain import rules as rules_module
from app.domain.enums import InvoiceStatus, RuleCode, Severity, SubscriptionStatus
from app.domain.models import Customer, Invoice, InvoiceLine, Subscription
from app.domain.money import Money
from app.domain.rules import (
    detect_duplicate_invoice,
    invoice_after_cancellation,
    proration_mismatch,
    suspected_impersonation,
    suspicious_usage_delta,
    unknown_customer,
    unknown_invoice,
)

SEPT = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)


def eur(minor: int) -> Money:
    return Money(amount_minor=minor, currency="EUR")


def line(
    amount_minor: int,
    *,
    unit_minor: int | None = None,
    start: datetime | None = SEPT,
    end: datetime | None = OCT,
    description: str = "Abonnement Enterprise",
) -> InvoiceLine:
    return InvoiceLine(
        description=description,
        quantity=Decimal(1),
        unit_amount=eur(unit_minor if unit_minor is not None else amount_minor),
        amount=eur(amount_minor),
        period_start=start,
        period_end=end,
    )


def invoice(
    invoice_id: str,
    *,
    total_minor: int = 9900,
    subscription_id: str | None = "sub_77",
    status: InvoiceStatus = InvoiceStatus.PAID,
    issued_at: datetime = SEPT,
    lines: list[InvoiceLine] | None = None,
) -> Invoice:
    return Invoice(
        id=invoice_id,
        customer_id="cus_acme",
        subscription_id=subscription_id,
        total=eur(total_minor),
        status=status,
        issued_at=issued_at,
        lines=lines if lines is not None else [line(total_minor)],
    )


def subscription(
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    cancelled_at: datetime | None = None,
) -> Subscription:
    return Subscription(
        id="sub_77",
        customer_id="cus_acme",
        status=status,
        plan_code="enterprise_monthly",
        current_period_start=SEPT,
        current_period_end=OCT,
        cancelled_at=cancelled_at,
    )


class TestDuplicateInvoice:
    def test_fires_on_same_period_same_amount(self):
        """Demo case A: ACME billed twice for September."""
        first = invoice("inv_001")
        second = invoice("inv_002")

        finding = detect_duplicate_invoice(second, [first, second])

        assert finding is not None
        assert finding.code is RuleCode.DUPLICATE_INVOICE
        assert finding.severity is Severity.HIGH
        assert finding.evidence["invoice_1"] == "inv_001"
        assert finding.evidence["invoice_2"] == "inv_002"

    def test_silent_on_legitimate_overage(self):
        """Demo case B: subscription plus a real usage overage.

        Two charges in one month, neither of them a duplicate. This is the
        test that keeps the agent from refunding by reflex.
        """
        subscription_invoice = invoice("inv_010", total_minor=9900)
        overage = invoice("inv_011", total_minor=4200)

        assert detect_duplicate_invoice(overage, [subscription_invoice, overage]) is None

    def test_silent_when_periods_do_not_overlap(self):
        august = invoice(
            "inv_008",
            lines=[line(9900, start=datetime(2026, 8, 1, tzinfo=UTC), end=SEPT)],
        )
        september = invoice("inv_009")

        assert detect_duplicate_invoice(september, [august, september]) is None

    def test_silent_on_a_different_subscription(self):
        other = invoice("inv_020", subscription_id="sub_88")
        target = invoice("inv_021")

        assert detect_duplicate_invoice(target, [other, target]) is None

    def test_silent_when_the_twin_was_voided(self):
        """A cancelled duplicate never took the customer's money."""
        voided = invoice("inv_030", status=InvoiceStatus.VOID)
        target = invoice("inv_031")

        assert detect_duplicate_invoice(target, [voided, target]) is None

    def test_does_not_match_itself(self):
        target = invoice("inv_040")
        assert detect_duplicate_invoice(target, [target]) is None


class TestInvoiceAfterCancellation:
    def test_fires_when_billed_past_cancellation(self):
        sub = subscription(
            status=SubscriptionStatus.CANCELLED,
            cancelled_at=datetime(2026, 8, 15, tzinfo=UTC),
        )

        finding = invoice_after_cancellation(invoice("inv_050"), sub)

        assert finding is not None
        assert finding.code is RuleCode.INVOICE_AFTER_CANCELLATION
        assert finding.evidence["cancelled_at"] == "2026-08-15T00:00:00+00:00"

    def test_silent_on_the_final_period_before_cancellation(self):
        sub = subscription(
            status=SubscriptionStatus.CANCELLED,
            cancelled_at=datetime(2026, 9, 20, tzinfo=UTC),
        )

        assert invoice_after_cancellation(invoice("inv_051"), sub) is None

    def test_silent_on_an_active_subscription(self):
        assert invoice_after_cancellation(invoice("inv_052"), subscription()) is None


class TestSuspiciousUsageDelta:
    def test_fires_beyond_tolerance(self):
        finding = suspicious_usage_delta(Decimal(1000), Decimal(5000))

        assert finding is not None
        assert finding.code is RuleCode.SUSPICIOUS_USAGE_DELTA
        assert finding.evidence["relative_delta"] == "4"

    def test_silent_within_tolerance(self):
        assert suspicious_usage_delta(Decimal(1000), Decimal(1100)) is None

    def test_fires_when_nothing_was_expected_but_something_was_billed(self):
        finding = suspicious_usage_delta(Decimal(0), Decimal(50))

        assert finding is not None
        assert finding.evidence["relative_delta"] is None

    def test_silent_when_nothing_expected_and_nothing_billed(self):
        assert suspicious_usage_delta(Decimal(0), Decimal(0)) is None


class TestProrationMismatch:
    def test_fires_when_a_half_month_is_billed_in_full(self):
        """Demo case D: plan change mid-month, full month charged."""
        half_month = line(
            9900,
            unit_minor=9900,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
            description="Enterprise, du 16/09 au 30/09",
        )

        finding = proration_mismatch(invoice("inv_060", lines=[half_month]), subscription())

        assert finding is not None
        assert finding.code is RuleCode.PRORATION_MISMATCH
        assert finding.evidence["expected_minor"] == 4950
        assert finding.evidence["billed_minor"] == 9900

    def test_silent_on_correct_proration(self):
        half_month = line(
            4950,
            unit_minor=9900,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
        )

        assert proration_mismatch(invoice("inv_061", lines=[half_month]), subscription()) is None

    def test_tolerates_one_minor_unit_of_provider_rounding(self):
        third = line(
            3300,
            unit_minor=9900,
            start=datetime(2026, 9, 21, tzinfo=UTC),
            end=OCT,  # 10 of 30 days -> 3300
        )

        assert proration_mismatch(invoice("inv_062", lines=[third]), subscription()) is None

    def test_silent_on_a_full_period_line(self):
        """Full-period lines are not prorated; a different rule's business."""
        assert proration_mismatch(invoice("inv_063"), subscription()) is None

    # A plan change mid-period, the way Stripe bills it: two lines, each at the
    # full price of its own plan, the unused time on the old plan credited with
    # a negative amount. Stripe docs, "Prorations", checked 2026-10-03.

    def test_silent_on_a_correct_credit_for_unused_time(self):
        unused_pro = line(
            -2450,
            unit_minor=4900,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
            description="Unused time on Pro",
        )

        assert proration_mismatch(invoice("inv_064", lines=[unused_pro]), subscription()) is None

    def test_fires_on_a_credit_that_covers_the_whole_month(self):
        unused_pro = line(
            -4900,
            unit_minor=4900,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
            description="Unused time on Pro",
        )

        finding = proration_mismatch(invoice("inv_065", lines=[unused_pro]), subscription())

        assert finding is not None
        assert finding.code is RuleCode.PRORATION_MISMATCH

    def test_silent_on_a_correct_plan_change_billed_as_two_lines(self):
        """Pro at 49 to Enterprise at 99 on the 16th: -24.50 + 49.50 = 25.00."""
        change = [
            line(
                -2450,
                unit_minor=4900,
                start=datetime(2026, 9, 16, tzinfo=UTC),
                end=OCT,
                description="Unused time on Pro",
            ),
            line(
                4950,
                unit_minor=9900,
                start=datetime(2026, 9, 16, tzinfo=UTC),
                end=OCT,
                description="Remaining time on Enterprise",
            ),
        ]

        target = invoice("inv_066", total_minor=2500, lines=change)

        assert proration_mismatch(target, subscription()) is None

    def test_silent_on_a_proration_computed_to_the_second(self):
        """Stripe prorates to the second, from the moment of the change. At
        10:00 on the 16th, 350 of the period's 720 hours remain: 99 x 350/720
        = 48.13. Counting whole days instead (14 of 30, truncated) expects
        46.20 and flags a correct line."""
        remaining = line(
            4813,
            unit_minor=9900,
            start=datetime(2026, 9, 16, 10, tzinfo=UTC),
            end=OCT,
            description="Remaining time on Enterprise",
        )

        assert proration_mismatch(invoice("inv_067", lines=[remaining]), subscription()) is None

    def test_a_net_proration_line_is_flagged_because_it_cannot_be_checked(self):
        """A known limit, kept visible. One net line of 25.00 for a half month
        carries no full plan price, so the rule reads 25.00 as one and expects
        12.50. Telling it from case D -- a half month billed at the full price
        -- would take the plan prices, which the provider port does not carry.
        The rule assumes provider-style lines, each at its plan's full price."""
        net = line(
            2500,
            unit_minor=2500,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
            description="Prorata Pro vers Enterprise",
        )

        assert proration_mismatch(invoice("inv_068", lines=[net]), subscription()) is not None


class TestUnknownInvoice:
    def test_fires_on_an_unknown_reference(self):
        finding = unknown_invoice("inv_999", [invoice("inv_001")])

        assert finding is not None
        assert finding.code is RuleCode.UNKNOWN_INVOICE
        assert finding.evidence["known_invoice_ids"] == ["inv_001"]

    def test_silent_on_a_known_reference(self):
        assert unknown_invoice("inv_001", [invoice("inv_001")]) is None



CUSTOMERS = [
    Customer(id="cus_acme", email="alice@acme.fr", display_name="ACME SAS"),
    Customer(id="cus_nord", email="bruno@transports-nord.fr", display_name="Transports Nord"),
    Customer(id="cus_lyon", email="chloe@lyon-industries.fr", display_name="Lyon Industries"),
    Customer(id="cus_solo", email="dan.solo@gmail.com", display_name=None),
]


class TestUnknownCustomer:
    def test_fires_when_the_sender_matches_no_customer(self):
        finding = unknown_customer("nobody@example.com", None)

        assert finding is not None
        assert finding.code is RuleCode.UNKNOWN_CUSTOMER
        assert finding.evidence["sender_email"] == "nobody@example.com"

    def test_silent_when_the_sender_is_a_known_customer(self):
        assert unknown_customer("alice@acme.fr", CUSTOMERS[0]) is None


class TestSuspectedImpersonation:
    """An unknown sender is not a fraud by itself. Reaching for a known
    customer's account -- by its invoice, its name or its domain -- is."""

    def signals(self, finding) -> set[tuple[str, str]]:
        return {(s["signal"], s["customer_id"]) for s in finding.evidence["signals"]}

    def test_silent_when_nothing_points_at_a_known_customer(self):
        """The plain unknown sender gets a question, not an escalation."""
        finding = suspected_impersonation(
            "jean@exemple.org", "Ma dernière facture me semble trop élevée.", None, CUSTOMERS
        )

        assert finding is None

    def test_fires_on_a_known_invoice_reference(self):
        finding = suspected_impersonation(
            "jean.perso@gmail.com", "Merci de corriger la facture.", invoice("inv_002"), CUSTOMERS
        )

        assert finding is not None
        assert finding.code is RuleCode.SUSPECTED_IMPERSONATION
        assert finding.severity is Severity.HIGH
        assert self.signals(finding) == {("known_invoice", "cus_acme")}

    def test_fires_on_the_exact_domain_of_a_known_customer(self):
        """Most likely a colleague. Still, a human checks with the known contact."""
        finding = suspected_impersonation("compta@acme.fr", "Bonjour.", None, CUSTOMERS)

        assert finding is not None
        assert self.signals(finding) == {("same_domain", "cus_acme")}

    def test_a_subdomain_counts_as_the_same_domain(self):
        finding = suspected_impersonation("compta@factures.acme.fr", "Bonjour.", None, CUSTOMERS)

        assert finding is not None
        assert self.signals(finding) == {("same_domain", "cus_acme")}

    @pytest.mark.parametrize(
        "domain",
        [
            "acme.com",  # same name, other extension
            "acme-fr.com",  # the name, decorated
            "acme-facturation.fr",
            "acrne.fr",  # rn reads as m
            "transport-nord.fr",  # one letter off a long name
            "transp0rts-nord.fr",  # 0 reads as o
        ],
    )
    def test_fires_on_a_lookalike_domain(self, domain):
        finding = suspected_impersonation(f"compta@{domain}", "Bonjour.", None, CUSTOMERS)

        assert finding is not None
        assert {signal for signal, _ in self.signals(finding)} == {"lookalike_domain"}

    @pytest.mark.parametrize(
        "domain",
        [
            "acne.fr",  # one letter off a short name: too close to call
            "pacmen.fr",  # contains the name, does not start with it
            "nord-conseil.fr",  # shares a word, not the name
            "industries-lyonnaises.fr",
        ],
    )
    def test_silent_on_an_unrelated_domain(self, domain):
        assert suspected_impersonation(f"jean@{domain}", "Bonjour.", None, CUSTOMERS) is None

    def test_fires_on_a_company_name_in_the_message(self):
        finding = suspected_impersonation(
            "jean.perso@gmail.com",
            "Je suis le comptable d'Acme, merci de m'envoyer nos factures.",
            None,
            CUSTOMERS,
        )

        assert finding is not None
        assert self.signals(finding) == {("company_name", "cus_acme")}

    def test_the_company_name_is_matched_without_accents_or_case(self):
        finding = suspected_impersonation(
            "jean.perso@gmail.com", "Facture de LYON INDUSTRIES.", None, CUSTOMERS
        )

        assert finding is not None
        assert self.signals(finding) == {("company_name", "cus_lyon")}

    def test_a_company_name_inside_another_word_does_not_count(self):
        finding = suspected_impersonation(
            "jean@exemple.org", "Une facture acmeuse.", None, CUSTOMERS
        )

        assert finding is None

    def test_a_webmail_domain_is_nobodys_domain(self):
        """A customer on gmail.com does not make every gmail.com sender suspect."""
        assert suspected_impersonation("jean@gmail.com", "Bonjour.", None, CUSTOMERS) is None

    def test_reports_every_signal_found(self):
        finding = suspected_impersonation(
            "compta@acme-fr.com", "Facture ACME.", invoice("inv_002"), CUSTOMERS
        )

        assert finding is not None
        assert self.signals(finding) == {
            ("known_invoice", "cus_acme"),
            ("lookalike_domain", "cus_acme"),
            ("company_name", "cus_acme"),
        }


@pytest.mark.parametrize("code", list(RuleCode))
def test_every_rule_code_is_emitted_somewhere(code):
    """No code may sit in the enum with no rule behind it.

    The audit log, the action policy and the evaluation suite all key off
    these codes; a declared-but-never-emitted one is a dead branch waiting to
    confuse whoever reads a run two months from now.

    The investigation module counts as a source: it emits the input checks
    that are not billing rules, such as a message too long for the engine;
    so does the automated-mail check (ADR-011).
    """
    sources = "".join(
        (Path(module.__file__).read_text(encoding="utf-8"))
        for module in (rules_module, injection_module, automated_module, investigation_module)
    )
    assert f"RuleCode.{code.name}" in sources

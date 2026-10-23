"""ADR-003: every rule gets a case that fires and a case that must not.

The must-not cases matter more. An agent that finds a duplicate everywhere is
worse than no agent at all -- it refunds by reflex. Scenario B of the demo
exists for exactly this reason.
"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.application import investigate_dispute as investigation_module
from app.domain import automated as automated_module
from app.domain import injection as injection_module
from app.domain import rules as rules_module
from app.domain.enums import (
    InvoiceStatus,
    PaymentStatus,
    RuleCode,
    Severity,
    SubscriptionStatus,
)
from app.domain.models import (
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceOwner,
    Payment,
    Subscription,
)
from app.domain.money import Money
from app.domain.rules import (
    checks_not_evaluable,
    cited_invoice_conflict,
    currency_mismatch,
    detect_duplicate_invoice,
    duplicate_payment,
    invoice_after_cancellation,
    proration_mismatch,
    suspected_impersonation,
    suspicious_usage_delta,
    unknown_customer,
    unknown_invoice,
)
from app.providers.fake.adapter import FakeBillingProvider

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
    start: datetime = SEPT,
    end: datetime = OCT,
    interval: str | None = "month",
    interval_count: int | None = 1,
    anchor: datetime | None = "start",
) -> Subscription:
    return Subscription(
        id="sub_77",
        customer_id="cus_acme",
        status=status,
        plan_code="enterprise_monthly",
        current_period_start=start,
        current_period_end=end,
        cancelled_at=cancelled_at,
        billing_interval=interval,
        billing_interval_count=interval_count,
        billing_anchor=start if anchor == "start" else anchor,
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
            "jean.perso@gmail.com",
            "Merci de corriger la facture.",
            InvoiceOwner("inv_002", "cus_acme"),
            CUSTOMERS,
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
            "compta@acme-fr.com", "Facture ACME.", InvoiceOwner("inv_002", "cus_acme"), CUSTOMERS
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


class TestCurrencyMismatch:
    """Taxonomy S10: where amounts must be compared across currencies, say so."""

    def usd(self, invoice_id="inv_002"):
        return invoice(invoice_id).model_copy(
            update={"total": Money(amount_minor=9900, currency="USD")}
        )

    def test_a_same_period_invoice_in_another_currency(self):
        finding = currency_mismatch(invoice("inv_001"), [invoice("inv_001"), self.usd()], [])
        assert finding.code is RuleCode.CURRENCY_MISMATCH
        assert finding.evidence["clashes"] == [{"invoice_id": "inv_002", "currency": "USD"}]

    def test_the_duplicate_rule_does_not_conclude_across_currencies(self):
        assert detect_duplicate_invoice(invoice("inv_001"), [self.usd()]) is None

    def test_a_payment_in_another_currency(self):
        paid = Payment(
            id="pay_1",
            invoice_id="inv_001",
            amount=Money(amount_minor=9900, currency="USD"),
            status=PaymentStatus.SUCCEEDED,
        )
        finding = currency_mismatch(invoice("inv_001"), [], [paid])
        assert finding.evidence["clashes"] == [{"payment_id": "pay_1", "currency": "USD"}]

    def test_a_line_in_another_currency_and_proration_stays_silent(self):
        foreign = InvoiceLine(
            description="Pro",
            quantity=Decimal(1),
            unit_amount=Money(amount_minor=9900, currency="USD"),
            amount=Money(amount_minor=4950, currency="USD"),
            period_start=SEPT,
            period_end=datetime(2026, 9, 16, tzinfo=UTC),
        )
        target = invoice("inv_001", lines=[foreign])
        assert currency_mismatch(target, [], []).code is RuleCode.CURRENCY_MISMATCH
        assert proration_mismatch(target, subscription()) is None

    @pytest.mark.parametrize(
        "other_update",
        [
            {"subscription_id": "sub_other"},
            {"status": InvoiceStatus.VOID},
        ],
        ids=["other-subscription", "void"],
    )
    def test_not_a_comparison_the_invoice_needs(self, other_update):
        other = self.usd().model_copy(update=other_update)
        assert currency_mismatch(invoice("inv_001"), [other], []) is None

    def test_another_period_in_another_currency_is_fine(self):
        other = self.usd().model_copy(
            update={
                "lines": [
                    line(
                        9900,
                        start=datetime(2026, 7, 1, tzinfo=UTC),
                        end=datetime(2026, 8, 1, tzinfo=UTC),
                    )
                ]
            }
        )
        assert currency_mismatch(invoice("inv_001"), [other], []) is None


class TestDuplicatePayment:
    def paid(self, payment_id, minor=9900, status=PaymentStatus.SUCCEEDED):
        return Payment(id=payment_id, invoice_id="inv_001", amount=eur(minor), status=status)

    def test_two_distinct_full_payments(self):
        finding = duplicate_payment(invoice("inv_001"), [self.paid("p1"), self.paid("p2")])
        assert finding.code is RuleCode.DUPLICATE_PAYMENT
        assert finding.evidence["payment_ids"] == ["p1", "p2"]

    @pytest.mark.parametrize(
        "payments",
        [
            lambda t: [t.paid("p1")],
            lambda t: [t.paid("p1"), t.paid("p1")],
            lambda t: [t.paid("p1"), t.paid("p2", minor=4950)],
            lambda t: [t.paid("p1"), t.paid("p2", status=PaymentStatus.REFUNDED)],
        ],
        ids=["one", "same-id-twice", "partial", "second-refunded"],
    )
    def test_not_a_duplicate(self, payments):
        assert duplicate_payment(invoice("inv_001"), payments(self)) is None


class TestCitedInvoiceConflict:
    """Taxonomy S6: only a conflict is flagged."""

    def test_only_other_invoices_are_cited(self):
        finding = cited_invoice_conflict({"inv_001"}, "inv_002")
        assert finding.code is RuleCode.INVOICE_REFERENCE_CONFLICT
        assert finding.evidence == {"designated": "inv_002", "cited": ["inv_001"]}

    @pytest.mark.parametrize(
        "cited", [set(), {"inv_001", "inv_002"}], ids=["nothing-cited", "designated-among-others"]
    )
    def test_no_conflict(self, cited):
        assert cited_invoice_conflict(cited, "inv_002") is None

    def test_no_designated_invoice_is_no_check(self):
        assert cited_invoice_conflict({"inv_001"}, None) is None


class TestTheProviderNormalisesItsReferences:
    """Only the platform knows how its references are written."""

    @pytest.mark.parametrize("written", ["inv_002", "INV-002", "inv002", "Inv_002"])
    def test_every_spelling_is_the_same_invoice(self, written):
        provider = FakeBillingProvider()
        assert provider.invoice_references(f"Ma facture {written} est fausse.") == {"inv_002"}

    def test_free_text_is_not_a_reference(self):
        provider = FakeBillingProvider()
        assert provider.invoice_references("Ma facture de septembre, involontairement.") == set()


class TestProrationStatesItsAssumption:
    def test_the_finding_names_the_assumption(self):
        half = line(9900, start=SEPT, end=datetime(2026, 9, 16, tzinfo=UTC))
        finding = proration_mismatch(invoice("inv_001", lines=[half]), subscription())
        assert "prix plein" in finding.description
        assert finding.evidence["assumption"] == "each line carries the full price of its own plan"


class TestUsageDirection:
    @pytest.mark.parametrize(
        "expected, billed, direction", [("100", "200", "over"), ("200", "100", "under")]
    )
    def test_the_gap_says_which_way(self, expected, billed, direction):
        finding = suspicious_usage_delta(Decimal(expected), Decimal(billed))
        assert finding.evidence["direction"] == direction


class TestChecksNotEvaluable:
    """D14: a check that applies but cannot conclude says so; one that does
    not apply stays silent."""

    def gaps(self, target, sub="default", invoices=()):
        sub = subscription() if sub == "default" else sub
        finding = checks_not_evaluable(target, sub, list(invoices))
        return None if finding is None else finding.evidence["checks"]

    def test_everything_present_is_no_finding(self):
        assert self.gaps(invoice("inv_001")) is None

    def test_a_one_off_invoice_is_not_a_gap(self):
        assert self.gaps(invoice("inv_001", subscription_id=None), sub=None) is None

    def test_a_void_invoice_is_not_a_gap(self):
        assert self.gaps(invoice("inv_001", status=InvoiceStatus.VOID), sub=None) is None

    def test_an_invoice_without_period(self):
        bare = invoice("inv_001", lines=[line(9900, start=None, end=None)])
        gaps = self.gaps(bare, invoices=[invoice("inv_002")])
        assert {"check": "duplicate_invoice", "reason": "invoice_without_period"} in gaps

    def test_without_a_candidate_a_missing_period_changes_nothing_for_duplicates(self):
        bare = invoice("inv_001", lines=[line(9900, start=None, end=None)])
        assert {"check": "duplicate_invoice", "reason": "invoice_without_period"} not in (
            self.gaps(bare) or []
        )

    @pytest.mark.parametrize(
        "start, end",
        [(SEPT, SEPT), (OCT, SEPT)],
        ids=["empty-period", "backwards-period"],
    )
    def test_an_invalid_period_counts_as_none(self, start, end):
        broken = invoice("inv_002", lines=[line(9900, start=start, end=end)])
        gaps = self.gaps(invoice("inv_001"), invoices=[broken])
        assert {"check": "duplicate_invoice", "reason": "candidate_without_period"} in gaps
        own = self.gaps(invoice("inv_001", lines=[line(9900, start=start, end=end)]))
        assert {"check": "proration", "reason": "line_without_valid_period"} in own

    def test_cancelled_without_a_date(self):
        undated = subscription(status=SubscriptionStatus.CANCELLED, cancelled_at=None)
        gaps = self.gaps(invoice("inv_001"), sub=undated)
        expected = {"check": "billing_after_cancellation", "reason": "cancellation_date_missing"}
        assert gaps == [expected]

    def test_a_candidate_without_period(self):
        other = invoice("inv_002", lines=[line(9900, start=None, end=None)])
        gaps = self.gaps(invoice("inv_001"), invoices=[other])
        assert gaps == [{"check": "duplicate_invoice", "reason": "candidate_without_period"}]

    def test_a_candidate_with_another_total_is_no_gap(self):
        other = invoice("inv_002", total_minor=4900, lines=[line(4900, start=None, end=None)])
        assert self.gaps(invoice("inv_001"), invoices=[other]) is None

    def test_a_missing_subscription(self):
        gaps = self.gaps(invoice("inv_001"), sub=None)
        assert {"check": "billing_after_cancellation", "reason": "subscription_not_found"} in gaps
        assert {"check": "proration", "reason": "subscription_not_found"} in gaps

    def test_a_line_without_period_for_proration(self):
        lines = [line(9900), line(500, start=None, end=None)]
        gaps = self.gaps(invoice("inv_001", lines=lines))
        assert gaps == [{"check": "proration", "reason": "line_without_valid_period"}]

    def test_a_missing_period_no_longer_reads_as_not_a_duplicate(self):
        """The defect itself: the duplicate rule is silent, and now that
        silence is said to be ignorance."""
        bare = [line(9900, start=None, end=None)]
        target = invoice("inv_001", lines=bare)
        other = invoice("inv_002", lines=bare)
        assert detect_duplicate_invoice(target, [other]) is None
        assert checks_not_evaluable(target, subscription(), [other]) is not None


def utc(*parts) -> datetime:
    return datetime(*parts, tzinfo=UTC)


class TestLinesOutsideTheCurrentPeriod:
    """A line of another period than the current one -- arrears billed once
    the subscription renewed, a period after a cancellation, an older line --
    was measured against the current period's length: a 30-day month read as
    a proration against a 31-day one (Stripe contract run, 2026-10-23).

    Now a line inside the current period is measured as before. One outside
    covers whole periods -- nothing to prorate -- only when both its bounds
    fall on the subscription's schedule: its original anchor, stepped by its
    cadence, months clamped to their last day as billing platforms clamp
    them. Any other line outside is said not evaluable, never passed in
    silence. Both functions are checked together.
    """

    def judged(self, lines, sub):
        target = invoice(
            "inv_070", total_minor=sum(x.amount.amount_minor for x in lines), lines=lines
        )
        mismatch = proration_mismatch(target, sub)
        gap = checks_not_evaluable(target, sub, [])
        gaps = gap.evidence["checks"] if gap is not None else []
        return mismatch, {"check": "proration", "reason": "line_outside_current_period"} in gaps

    def monthly_on_the_22nd(self, start, end, **changes):
        return subscription(start=start, end=end, anchor=utc(2026, 1, 22), **changes)

    def test_arrears_for_a_whole_earlier_month_are_no_proration(self):
        renewed = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        arrears = line(2350, start=utc(2026, 9, 22), end=utc(2026, 10, 22))
        assert self.judged([arrears], renewed) == (None, False)

    def test_a_whole_month_after_a_cancellation_is_no_proration(self):
        ended = self.monthly_on_the_22nd(
            utc(2026, 8, 22),
            utc(2026, 9, 22),
            status=SubscriptionStatus.CANCELLED,
            cancelled_at=utc(2026, 9, 21),
        )
        after = line(4900, start=utc(2026, 9, 22), end=utc(2026, 10, 22))
        assert self.judged([after], ended) == (None, False)

    def test_one_month_of_a_yearly_subscription_is_not_a_whole_period(self):
        yearly = subscription(
            start=utc(2026, 1, 22), end=utc(2027, 1, 22), interval="year", anchor=utc(2026, 1, 22)
        )
        month = line(9900, start=utc(2025, 9, 22), end=utc(2025, 10, 22))
        assert self.judged([month], yearly) == (None, True)

    def test_a_whole_earlier_year_is_a_whole_period(self):
        yearly = subscription(
            start=utc(2026, 1, 22), end=utc(2027, 1, 22), interval="year", anchor=utc(2025, 1, 22)
        )
        year = line(99000, start=utc(2025, 1, 22), end=utc(2026, 1, 22))
        assert self.judged([year], yearly) == (None, False)

    def test_an_anchor_on_the_31st_is_not_mistaken_for_the_28th(self):
        """February clamps the 31st to the 28th; January's 28th is no step."""
        end_of_month = subscription(
            start=utc(2026, 2, 28), end=utc(2026, 3, 31), anchor=utc(2026, 1, 31)
        )
        wrong = line(9900, start=utc(2026, 1, 28), end=utc(2026, 2, 28))
        right = line(9900, start=utc(2026, 1, 31), end=utc(2026, 2, 28))
        assert self.judged([wrong], end_of_month) == (None, True)
        assert self.judged([right], end_of_month) == (None, False)

    def test_every_third_month_steps_by_three(self):
        quarterly = subscription(
            start=utc(2026, 7, 22), end=utc(2026, 10, 22), interval_count=3, anchor=utc(2026, 1, 22)
        )
        quarter = line(29700, start=utc(2026, 4, 22), end=utc(2026, 7, 22))
        between = line(9900, start=utc(2026, 5, 22), end=utc(2026, 6, 22))
        assert self.judged([quarter], quarterly) == (None, False)
        assert self.judged([between], quarterly) == (None, True)

    def test_weeks_step_by_seven_days(self):
        weekly = subscription(
            start=utc(2026, 9, 21), end=utc(2026, 9, 28), interval="week", anchor=utc(2026, 9, 7)
        )
        fortnight = line(1000, start=utc(2026, 9, 7), end=utc(2026, 9, 21))
        off = line(1000, start=utc(2026, 9, 8), end=utc(2026, 9, 21))
        assert self.judged([fortnight], weekly) == (None, False)
        assert self.judged([off], weekly) == (None, True)

    def test_the_same_instants_in_another_offset_are_the_same_steps(self):
        renewed = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        paris = timezone(timedelta(hours=2))
        arrears = line(
            2350,
            start=datetime(2026, 9, 22, 2, tzinfo=paris),
            end=datetime(2026, 10, 22, 2, tzinfo=paris),
        )
        assert self.judged([arrears], renewed) == (None, False)

    def test_another_hour_on_the_anchor_day_is_no_step(self):
        renewed = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        late = line(2350, start=utc(2026, 9, 22), end=utc(2026, 10, 22, 12))
        assert self.judged([late], renewed) == (None, True)

    def test_without_a_known_cadence_a_line_outside_is_said(self):
        unknown = subscription(
            start=utc(2026, 10, 22),
            end=utc(2026, 11, 22),
            interval=None,
            interval_count=None,
            anchor=None,
        )
        arrears = line(2350, start=utc(2026, 9, 22), end=utc(2026, 10, 22))
        assert self.judged([arrears], unknown) == (None, True)

    def test_a_partial_credit_outside_is_said_never_judged(self):
        renewed = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        credit = line(-2450, unit_minor=4900, start=utc(2026, 10, 7), end=utc(2026, 10, 22))
        charge = line(4900, start=utc(2026, 10, 22), end=utc(2026, 11, 22))
        assert self.judged([credit, charge], renewed) == (None, True)

    def test_a_line_across_the_current_start_is_said(self):
        renewed = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        across = line(9900, start=utc(2026, 10, 7), end=utc(2026, 11, 7))
        assert self.judged([across], renewed) == (None, True)

    def test_the_current_period_itself_is_neither(self):
        renewed = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        whole = line(9900, start=utc(2026, 10, 22), end=utc(2026, 11, 22))
        assert self.judged([whole], renewed) == (None, False)

    def test_a_partial_line_inside_is_still_measured(self):
        current = self.monthly_on_the_22nd(utc(2026, 10, 22), utc(2026, 11, 22))
        half = line(9900, unit_minor=9900, start=utc(2026, 11, 7), end=utc(2026, 11, 22))
        mismatch, said = self.judged([half], current)
        assert mismatch is not None and mismatch.code is RuleCode.PRORATION_MISMATCH
        assert not said

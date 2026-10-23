"""Stripe payloads to domain objects, one test per rule (plan section 4).

No SDK and no network: the mapping is pure functions on the payloads of
`tests/unit/stripe_payloads`, API version 2026-07-29.dahlia.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.money import Money
from app.providers.base import ProviderUnsupported
from app.providers.stripe import mapping
from tests.unit import stripe_payloads as stripe
from tests.unit.stripe_payloads import AUG_31, OCT_1, SEP_1, SEP_16

SEPTEMBER = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


def refused(reason: str):
    return pytest.raises(ProviderUnsupported, match=f"^{reason}$")


def invoice(lines=None, prices=None, meters=None, **changes):
    payload = stripe.invoice(lines, **changes)
    return mapping.invoice(
        payload,
        prices if prices is not None else {stripe.PRICE: stripe.price()},
        meters if meters is not None else {},
    )


class TestCustomer:
    def test_identifier_address_as_written_currency_upper_case_and_name(self):
        customer = mapping.customer(stripe.customer())
        assert customer.id == stripe.CUSTOMER
        assert customer.email == "  Billing@Duplicate.Example.test "
        assert customer.currency == "EUR"
        assert customer.display_name == "ACME SAS"

    def test_a_customer_never_billed_has_no_currency(self):
        """Stripe sets the currency at the first invoice; the domain allows none."""
        assert mapping.customer(stripe.customer(currency=None)).currency is None

    def test_no_name_stays_none(self):
        assert mapping.customer(stripe.customer(name=None)).display_name is None

    def test_no_address_is_a_missing_field(self):
        with refused("missing_field"):
            mapping.customer(stripe.customer(email=None))

    def test_the_address_compared_is_trimmed_and_lower_cased(self):
        assert mapping.address(stripe.customer()) == "billing@duplicate.example.test"

    def test_no_address_is_never_a_match(self):
        assert mapping.address(stripe.customer(email=None)) is None
        assert mapping.address(stripe.customer(email="   ")) is None

    def test_customers_are_ordered_by_creation_then_identifier(self):
        assert mapping.customer_order(stripe.customer()) == (stripe.AUG_1, stripe.CUSTOMER)


class TestSubscription:
    def test_one_item_gives_the_period_the_plan_and_the_status(self):
        subscription = mapping.subscription(stripe.subscription())
        assert subscription.id == stripe.SUBSCRIPTION
        assert subscription.customer_id == stripe.CUSTOMER
        assert subscription.status is SubscriptionStatus.ACTIVE
        assert subscription.plan_code == "pro_monthly"
        assert (subscription.current_period_start, subscription.current_period_end) == SEPTEMBER
        assert subscription.cancelled_at is None

    def test_the_cadence_and_the_anchor(self):
        subscription = mapping.subscription(stripe.subscription(billing_cycle_anchor=SEP_1))
        assert (subscription.billing_interval, subscription.billing_interval_count) == ("month", 1)
        assert subscription.billing_anchor == SEPTEMBER[0]

    def test_a_yearly_price_every_two_years(self):
        recurring = {
            "interval": "year",
            "interval_count": 2,
            "meter": None,
            "usage_type": "licensed",
        }
        payload = stripe.subscription([stripe.item(stripe.price(recurring=recurring))])
        subscription = mapping.subscription(payload)
        assert (subscription.billing_interval, subscription.billing_interval_count) == ("year", 2)

    def test_a_configured_anchor_leaves_the_cadence_unknown(self):
        """`billing_cycle_anchor_config` (a day of the month, say the 31st)
        steps periods by a day the timestamp alone may not show: not
        represented, so said unknown rather than guessed."""
        payload = stripe.subscription(
            billing_cycle_anchor=SEP_1,
            billing_cycle_anchor_config={"day_of_month": 31, "hour": 0, "minute": 0, "second": 0},
        )
        subscription = mapping.subscription(payload)
        assert subscription.billing_interval is None
        assert subscription.billing_interval_count is None

    def test_no_anchor_and_no_cadence_are_unknown_never_guessed(self):
        price = stripe.price(recurring=None)
        payload = stripe.subscription([stripe.item(price)], billing_cycle_anchor=None)
        subscription = mapping.subscription(payload)
        assert subscription.billing_interval is None
        assert subscription.billing_interval_count is None
        assert subscription.billing_anchor is None

    def test_the_plan_is_the_price_identifier_without_a_lookup_key(self):
        payload = stripe.subscription([stripe.item(stripe.price(lookup_key=None))])
        assert mapping.subscription(payload).plan_code == stripe.PRICE

    def test_items_sharing_one_price_and_one_period_are_supported(self):
        items = [stripe.item(id="si_1"), stripe.item(id="si_2")]
        assert mapping.subscription(stripe.subscription(items)).plan_code == "pro_monthly"

    def test_items_on_different_periods_are_refused(self):
        items = [stripe.item(id="si_1"), stripe.item(id="si_2", start=SEP_16)]
        with refused("mixed_item_periods"):
            mapping.subscription(stripe.subscription(items))

    def test_items_on_different_prices_are_refused(self):
        items = [stripe.item(id="si_1"), stripe.item(stripe.metered_price(), id="si_2")]
        with refused("multiple_prices"):
            mapping.subscription(stripe.subscription(items))

    def test_no_item_is_a_missing_field(self):
        with refused("missing_field"):
            mapping.subscription(stripe.subscription([]))

    def test_a_period_that_is_not_real_is_refused(self):
        payload = stripe.subscription([stripe.item(start=OCT_1, end=SEP_1)])
        with refused("unrepresentable_period"):
            mapping.subscription(payload)

    @pytest.mark.parametrize("status", ["active", "trialing", "past_due", "paused"])
    def test_these_statuses_map_to_themselves(self, status):
        subscription = mapping.subscription(stripe.subscription(status=status))
        assert subscription.status.value == status
        assert subscription.cancelled_at is None

    def test_canceled_carries_the_date_it_ended_not_the_request(self):
        payload = stripe.subscription(status="canceled", canceled_at=SEP_16, ended_at=AUG_31)
        subscription = mapping.subscription(payload)
        assert subscription.status is SubscriptionStatus.CANCELLED
        assert subscription.cancelled_at == datetime(2026, 8, 31, tzinfo=UTC)

    def test_canceled_without_an_end_is_refused(self):
        with refused("cancellation_without_end"):
            mapping.subscription(stripe.subscription(status="canceled", canceled_at=SEP_16))

    def test_a_cancellation_scheduled_later_keeps_the_status_and_no_date(self):
        payload = stripe.subscription(
            cancel_at=OCT_1, cancel_at_period_end=True, canceled_at=SEP_16
        )
        subscription = mapping.subscription(payload)
        assert subscription.status is SubscriptionStatus.ACTIVE
        assert subscription.cancelled_at is None

    @pytest.mark.parametrize("status", ["incomplete", "incomplete_expired", "unpaid", "new"])
    def test_other_statuses_are_refused(self, status):
        with refused("subscription_status"):
            mapping.subscription(stripe.subscription(status=status))


class TestInvoice:
    def test_identifier_total_currency_subscription_and_line(self):
        mapped = invoice()
        assert mapped.id == stripe.INVOICE
        assert mapped.customer_id == stripe.CUSTOMER
        assert mapped.subscription_id == stripe.SUBSCRIPTION
        assert mapped.total == Money(amount_minor=4900, currency="EUR")
        [line] = mapped.lines
        assert line.amount == Money(amount_minor=4900, currency="EUR")
        assert line.unit_amount == Money(amount_minor=4900, currency="EUR")
        assert line.quantity == Decimal("1")
        assert (line.period_start, line.period_end) == SEPTEMBER
        assert line.metric is None

    @pytest.mark.parametrize("status", [s.value for s in InvoiceStatus])
    def test_statuses_map_to_themselves(self, status):
        assert invoice(status=status).status.value == status

    def test_an_unknown_status_is_refused(self):
        with refused("missing_field"):
            invoice(status="reverted")

    def test_issued_when_finalised(self):
        assert invoice().issued_at == datetime(2026, 9, 1, 1, tzinfo=UTC)

    def test_a_draft_is_issued_when_created(self):
        transitions = {"finalized_at": None, "marked_uncollectible_at": None}
        draft = invoice(status="draft", status_transitions=transitions)
        assert draft.issued_at == datetime(2026, 9, 1, tzinfo=UTC)

    def test_a_finalised_invoice_without_its_date_is_refused(self):
        with refused("missing_field"):
            invoice(status_transitions={"finalized_at": None})

    def test_an_invoice_outside_a_subscription_has_none(self):
        assert invoice(parent=None).subscription_id is None

    @pytest.mark.parametrize("behavior", ["exclusive", "inclusive"])
    def test_tax_on_the_invoice_is_refused(self, behavior):
        with refused("invoice_tax"):
            invoice(total_taxes=[stripe.tax(behavior=behavior)])

    def test_tax_on_a_line_is_refused(self):
        with refused("invoice_tax"):
            invoice([stripe.line(taxes=[stripe.tax(behavior="inclusive")])])

    def test_a_tax_entry_of_zero_is_no_tax(self):
        assert invoice(total_taxes=[stripe.tax(amount=0)]).total.amount_minor == 4900

    def test_a_discount_on_the_invoice_is_refused(self):
        with refused("invoice_discount"):
            invoice(total_discount_amounts=[stripe.discount_amount()])

    def test_a_discount_on_a_line_is_refused(self):
        with refused("invoice_discount"):
            invoice([stripe.line(discount_amounts=[stripe.discount_amount()])])

    def test_a_pre_tax_discount_on_the_invoice_is_refused(self):
        with refused("invoice_discount"):
            invoice(total_pretax_credit_amounts=[stripe.pretax_credit()])

    def test_a_pre_tax_discount_on_a_line_is_refused(self):
        with refused("invoice_discount"):
            invoice([stripe.line(pretax_credit_amounts=[stripe.pretax_credit()])])

    @pytest.mark.parametrize("kind", ["credit_balance_transaction", "a_kind_not_known_yet"])
    def test_a_pre_tax_credit_on_the_invoice_is_refused(self, kind):
        with refused("invoice_credit"):
            invoice(total_pretax_credit_amounts=[stripe.pretax_credit(kind=kind)])

    def test_a_pre_tax_credit_on_a_line_is_refused(self):
        credit = stripe.pretax_credit(kind="credit_balance_transaction")
        with refused("invoice_credit"):
            invoice([stripe.line(pretax_credit_amounts=[credit])])

    def test_a_pre_tax_credit_of_zero_is_no_credit(self):
        credit = stripe.pretax_credit(0, kind="credit_balance_transaction")
        assert invoice(total_pretax_credit_amounts=[credit]).lines

    def test_a_pre_tax_discount_of_zero_is_no_discount(self):
        assert invoice(total_pretax_credit_amounts=[stripe.pretax_credit(0)]).lines

    def test_a_discount_of_zero_is_no_discount(self):
        assert invoice([stripe.line(discount_amounts=[stripe.discount_amount(0)])]).lines

    def test_a_negative_line_is_refused(self):
        with refused("negative_line"):
            invoice([stripe.line(), stripe.line(id="il_credit", amount=-4900)])

    def test_a_fractional_unit_price_is_refused(self):
        with refused("fractional_unit_price"):
            invoice([stripe.line(unit_amount_decimal="4900.5")])

    @pytest.mark.parametrize("whole", ["4900", "4900.000"])
    def test_a_whole_unit_price_is_whole_however_written(self, whole):
        [line] = invoice([stripe.line(unit_amount_decimal=whole)]).lines
        assert line.unit_amount.amount_minor == 4900

    def test_a_line_without_a_description_has_an_empty_one(self):
        assert invoice([stripe.line(description=None)]).lines[0].description == ""

    def test_a_line_keeps_its_description(self):
        assert invoice().lines[0].description == "1 x Pro (at EUR 49.00 / month)"

    def test_the_quantity_is_the_decimal_one_else_the_integer(self):
        assert invoice([stripe.line(quantity_decimal="2.5")]).lines[0].quantity == Decimal("2.5")
        assert invoice([stripe.line(quantity_decimal=None, quantity=3)]).lines[0].quantity == 3

    @pytest.mark.parametrize(
        "changes",
        [{"pricing": None}, {"unit_amount_decimal": None}, {"period": None}],
        ids=["pricing", "unit_amount", "period"],
    )
    def test_a_field_the_line_needs_is_never_guessed(self, changes):
        with refused("missing_field"):
            invoice([stripe.line(**changes)])

    def test_a_metered_line_carries_the_meters_event_name(self):
        metered = stripe.line(
            amount=4700, unit_amount_decimal="100", line_price=stripe.METERED_PRICE
        )
        mapped = invoice(
            [metered],
            prices={stripe.METERED_PRICE: stripe.metered_price()},
            meters={stripe.METER: stripe.meter()},
        )
        assert mapped.lines[0].metric == "api_call_blocks_100"

    def test_a_price_sent_expanded_is_read_in_place(self):
        expanded = stripe.line(line_price=stripe.metered_price())
        mapped = invoice([expanded], prices={}, meters={stripe.METER: stripe.meter()})
        assert mapped.lines[0].metric == "api_call_blocks_100"

    def test_the_prices_and_meters_a_mapping_needs_are_named(self):
        payload = stripe.invoice([stripe.line(), stripe.line(line_price=stripe.METERED_PRICE)])
        assert mapping.line_price_ids(payload) == {stripe.PRICE, stripe.METERED_PRICE}
        prices = {stripe.PRICE: stripe.price(), stripe.METERED_PRICE: stripe.metered_price()}
        assert mapping.line_meter_ids(payload, prices) == {stripe.METER}


class TestPayments:
    def payment(self, payload):
        [payment] = mapping.payments([payload])
        return payment

    def test_a_succeeded_payment_carries_what_was_collected(self):
        payment = self.payment(stripe.invoice_payment(amount_paid=4900, amount_requested=5000))
        assert payment.status is PaymentStatus.SUCCEEDED
        assert payment.amount == Money(amount_minor=4900, currency="EUR")
        assert payment.invoice_id == stripe.INVOICE
        assert payment.received_at == datetime(2026, 9, 1, 2, tzinfo=UTC)

    def test_a_fully_refunded_payment_is_refunded(self):
        charge = stripe.charge(amount_refunded=4900, refunded=True)
        payment = self.payment(stripe.invoice_payment(stripe.payment_intent(charge)))
        assert payment.status is PaymentStatus.REFUNDED
        assert payment.amount.amount_minor == 4900

    def test_a_partial_refund_is_refused(self):
        charge = stripe.charge(amount_refunded=1000)
        with refused("partial_refund"):
            mapping.payments([stripe.invoice_payment(stripe.payment_intent(charge))])

    def test_a_failed_collection_is_failed_and_never_money_received(self):
        charge = stripe.charge(status="failed", paid=False, captured=False, amount_captured=0)
        intent = stripe.payment_intent(charge, status="requires_payment_method", amount_received=0)
        payload = stripe.invoice_payment(
            intent,
            amount_paid=None,
            status="open",
            status_transitions={"canceled_at": None, "paid_at": None},
        )
        payment = self.payment(payload)
        assert payment.status is PaymentStatus.FAILED
        assert payment.amount.amount_minor == 4900
        assert payment.received_at is None

    def test_a_canceled_intent_is_failed(self):
        intent = stripe.payment_intent(None, status="canceled")
        payment = self.payment(stripe.invoice_payment(intent, amount_paid=None))
        assert payment.status is PaymentStatus.FAILED

    @pytest.mark.parametrize(
        "status", ["processing", "requires_action", "requires_capture", "requires_payment_method"]
    )
    def test_a_collection_in_progress_is_pending(self, status):
        intent = stripe.payment_intent(stripe.charge(status="pending"), status=status)
        payment = self.payment(stripe.invoice_payment(intent, amount_paid=None))
        assert payment.status is PaymentStatus.PENDING
        assert payment.amount.amount_minor == 4900
        assert payment.received_at is None

    def test_a_payment_not_yet_attempted_is_pending(self):
        intent = stripe.payment_intent(None, status="requires_payment_method")
        payment = self.payment(stripe.invoice_payment(intent, amount_paid=None))
        assert payment.status is PaymentStatus.PENDING

    def test_one_payment_across_two_invoices_is_refused(self):
        first = stripe.invoice_payment()
        second = stripe.invoice_payment(id="inpay_RbWorld0000002", invoice="in_1RbWorld00000002")
        with refused("multi_invoice_payment"):
            mapping.payments([first, second])

    @pytest.mark.parametrize(
        "payment",
        [
            {"type": "charge", "charge": "ch_RbWorld0000001", "payment_intent": None},
            {"type": "payment_record", "payment_record": "pr_Rb000001"},
            {"type": "payment_intent", "payment_intent": None},
        ],
        ids=["charge", "payment_record", "no_intent"],
    )
    def test_a_payment_the_domain_cannot_place_is_refused(self, payment):
        with refused("unplaced_payment"):
            mapping.payments([stripe.invoice_payment(payment=payment)])

    def test_an_intent_not_expanded_is_a_missing_field(self):
        with refused("missing_field"):
            mapping.payments([stripe.invoice_payment("pi_RbWorld0000001")])


class TestUsage:
    def subscriptions(self, *items_by_subscription):
        return [
            stripe.subscription(items, id=f"sub_Rb{n:012d}")
            for n, items in enumerate(items_by_subscription, start=1)
        ]

    def test_the_meter_of_the_one_metered_item(self):
        subscriptions = self.subscriptions([stripe.item(stripe.metered_price())])
        assert mapping.usage_meter("sub_Rb000000000001", subscriptions) == stripe.METER

    def test_no_metered_item_has_no_meter(self):
        subscriptions = self.subscriptions([stripe.item()])
        assert mapping.usage_meter("sub_Rb000000000001", subscriptions) is None

    def test_two_metered_items_have_no_meter(self):
        other = stripe.metered_price(id="price_RbOther00001")
        subscriptions = self.subscriptions(
            [stripe.item(stripe.metered_price()), stripe.item(other, id="si_2")]
        )
        assert mapping.usage_meter("sub_Rb000000000001", subscriptions) is None

    def test_a_meter_another_subscription_uses_is_not_read(self):
        subscriptions = self.subscriptions(
            [stripe.item(stripe.metered_price())],
            [stripe.item(stripe.metered_price(), id="si_2")],
        )
        assert mapping.usage_meter("sub_Rb000000000001", subscriptions) is None

    def test_a_period_on_whole_minutes_is_the_summary_window(self):
        assert mapping.summary_window(SEPTEMBER) == (SEP_1, OCT_1)

    def test_a_period_off_whole_minutes_is_refused(self):
        start, end = SEPTEMBER
        with refused("unrepresentable_period"):
            mapping.summary_window((start.replace(second=30), end))

    def test_summaries_are_added_up_over_the_period(self):
        usage = mapping.usage(
            "sub_x", "api_call_blocks_100", SEPTEMBER, [stripe.summary(40.0), stripe.summary(7.0)]
        )
        assert usage.quantity == Decimal("47")
        assert usage.metric == "api_call_blocks_100"
        assert (usage.period_start, usage.period_end) == SEPTEMBER

    def test_no_summary_is_unavailable_never_zero(self):
        assert mapping.usage("sub_x", "api_call_blocks_100", SEPTEMBER, []) is None

    def test_a_summary_of_zero_is_zero(self):
        usage = mapping.usage("sub_x", "api_call_blocks_100", SEPTEMBER, [stripe.summary(0.0)])
        assert usage.quantity == 0

    def test_a_fractional_aggregate_is_kept(self):
        usage = mapping.usage("sub_x", "gb_hours", SEPTEMBER, [stripe.summary(2.5)])
        assert usage.quantity == Decimal("2.5")


class TestReferences:
    def test_a_stripe_invoice_identifier_is_found_where_it_sits(self):
        text = "Ma facture in_1RbWorld00000001 est en double."
        assert mapping.references(text) == [(11, 30, "in_1RbWorld00000001")]

    @pytest.mark.parametrize(
        "text", ["facture inv_002", "in_short1234", "login_1RbWorld00000001", "INV-002"]
    )
    def test_anything_else_is_not_a_reference(self, text):
        assert mapping.references(text) == []


NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def written(payload):
    return mapping.provider_action(payload, observed_at=NOW)


class TestCreditNote:
    """A credit note, as the execution service checks it
    (specs/002-stripe-writes, FR-004 and FR-015)."""

    def test_what_it_made(self):
        made = written(stripe.credit_note())

        assert made.provider == "stripe"
        assert made.provider_action_id == stripe.CREDIT_NOTE
        assert (made.invoice_id, made.amount_minor, made.currency) == (stripe.INVOICE, 4900, "EUR")
        assert (made.idempotency_key, made.action_id) == (stripe.KEY, stripe.ACTION)
        assert made.action_type == "credit_note"
        assert made.test_mode is True
        assert made.created_at == datetime.fromtimestamp(SEP_16, UTC)
        assert made.observed_at == NOW

    def test_before_payment_it_lowers_what_is_due(self):
        assert written(stripe.credit_note(kind="pre_payment")).allocation == "reduce_due"

    def test_after_payment_without_a_refund_it_credits_the_balance(self):
        made = written(stripe.credit_note(kind="post_payment"))
        assert (made.allocation, made.status) == ("customer_balance", "succeeded")

    def test_after_payment_refunded_in_full_it_is_a_refund(self):
        made = written(stripe.credit_note(refunds=[stripe.credit_note_refund()]))
        assert (made.allocation, made.action_type) == ("refund", "refund")

    def test_partly_refunded_it_cannot_be_read(self):
        made = written(stripe.credit_note(refunds=[stripe.credit_note_refund(amount=1000)]))
        assert made.allocation is None

    def test_paid_out_of_band_it_cannot_be_read(self):
        made = written(stripe.credit_note(out_of_band_amount=4900))
        assert made.allocation is None

    @pytest.mark.parametrize(
        ("refund_status", "status"),
        [
            ("succeeded", "succeeded"),
            ("pending", "pending"),
            ("requires_action", "pending"),
            ("failed", "failed"),
            ("canceled", "failed"),
        ],
    )
    def test_a_refunds_status(self, refund_status, status):
        payload = stripe.credit_note(
            refunds=[stripe.credit_note_refund(stripe.refund(status=refund_status))]
        )
        assert written(payload).status == status

    def test_a_refund_not_expanded_leaves_the_status_unread(self):
        payload = stripe.credit_note(refunds=[stripe.credit_note_refund(stripe.REFUND)])
        assert written(payload).status is None

    def test_a_void_credit_note_failed(self):
        assert written(stripe.credit_note(status="void")).status == "failed"

    def test_a_live_credit_note_is_not_test_mode(self):
        assert written(stripe.credit_note(livemode=True)).test_mode is False

    def test_without_its_type_it_cannot_be_represented(self):
        with refused("missing_field"):
            written(stripe.credit_note(metadata={"rb_idempotency_key": stripe.KEY}))

    def test_without_its_key_it_cannot_be_represented(self):
        with refused("missing_field"):
            written(stripe.credit_note(metadata={"rb_action_type": "credit_note"}))

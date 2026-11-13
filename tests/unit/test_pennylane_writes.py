"""Pennylane writes credit notes on the declared sandbox, against a stubbed
transport, without network (specs/015-pennylane-credit-notes, plan section 3:
FR-002 to FR-006, FR-008, FR-011 on the adapter's side, FR-012, FR-013)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.models import TaxBreakdown
from app.domain.money import Money
from app.providers.base import (
    NotSent,
    PartialWrite,
    PlatformCannotWrite,
    ProviderOutcomeUnknown,
    ProviderRejected,
    ProviderUnavailable,
)
from app.providers.pennylane import mapping
from app.providers.pennylane.adapter import PennylaneBillingProvider
from tests.unit.pennylane_stub import (
    CAP_REFUSAL,
    TEST_EMAIL,
    credit_world,
    line,
    writing_provider,
)

KEY = "0f" * 32
REFERENCE = f"rb:{KEY}"
INVOICE = "pl_inv_600"
DRAFT = 9001
FORBIDDEN = ("email", "e_invoic", "einvoic", "e-invoic")


def approved(excl=10000, taxes=((Decimal(20), 2000),), total=12000):
    if not taxes:
        return None
    return TaxBreakdown(
        excluding_tax=Money(amount_minor=excl, currency="EUR"),
        taxes=tuple((rate, Money(amount_minor=a, currency="EUR")) for rate, a in taxes),
        total=Money(amount_minor=total, currency="EUR"),
    )


class Hook:
    """The execution service's `before_request`: counts, and raises from the
    `fail_at`-th call on."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls = 0
        self.fail_at = fail_at

    async def __call__(self) -> None:
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise NotSent("step_deadline")


async def credit(provider, *, amount=12000, breakdown="default", allocation="reduce_due",
                 hook=None, key=KEY):
    return await provider.create_credit_note(
        INVOICE, amount, "EUR", "The customer wrote: refund me, my card is 4242", key,
        action_id="act_1", allocation=allocation, send_by=None,
        breakdown=approved() if breakdown == "default" else breakdown,
        before_request=hook,
    )


def no_outward_path(account) -> None:
    """FR-012: no email or e-invoicing endpoint is ever requested."""
    assert not [p for _, p in account.calls if any(part in p.lower() for part in FORBIDDEN)]


# -- the happy paths (US 1, FR-003, FR-004) ---------------------------------------------


async def test_an_open_invoice_is_credited_in_fr004_order():
    account = credit_world("upcoming")
    provider = writing_provider(account)
    hook = Hook()
    made = await credit(provider, hook=hook)

    assert made.provider == "pennylane"
    assert made.provider_action_id == f"pl_inv_{DRAFT}"
    assert (made.status, made.allocation, made.partial) == ("succeeded", "reduce_due", None)
    assert (made.invoice_id, made.amount_minor, made.currency) == (INVOICE, 12000, "EUR")
    assert made.breakdown == approved()
    assert made.idempotency_key == KEY and made.action_id is None
    assert made.test_mode is True
    assert account.calls == [
        ("GET", "/me"),  # the write token's company
        ("GET", "/me"),  # the read token's company
        ("GET", "/customer_invoices/600"),
        ("GET", "/customer_invoices/600/invoice_lines"),
        ("GET", "/customers/7"),
        ("GET", "/customer_invoices"),
        ("GET", "/customers/7/electronic_billing_addresses"),
        ("POST", "/customer_invoices"),
        ("GET", f"/customer_invoices/{DRAFT}"),
        ("GET", f"/customer_invoices/{DRAFT}/invoice_lines"),
        ("POST", "/customer_invoices/600/link_credit_note"),
        ("GET", "/customers/7"),
        ("GET", "/customers/7/electronic_billing_addresses"),
        ("GET", f"/customer_invoices/{DRAFT}"),
        ("PUT", f"/customer_invoices/{DRAFT}/finalize"),
        ("GET", f"/customer_invoices/{DRAFT}"),
        ("GET", f"/customer_invoices/{DRAFT}/invoice_lines"),
        ("GET", "/customer_invoices/600"),
    ]
    # Before every request, the execution service's check (FR-011).
    assert hook.calls == len(account.calls)
    no_outward_path(account)


async def test_the_draft_is_dated_the_day_carries_the_key_and_nothing_of_the_dispute():
    account = credit_world("upcoming")
    await credit(writing_provider(account))
    method, path, body = account.sent[0]
    assert (method, path) == ("POST", "/customer_invoices")
    assert body["draft"] is True
    assert body["date"] == "2026-11-12"
    assert body["external_reference"] == REFERENCE
    assert body["currency"] == "EUR" and body["customer_id"] == 7
    assert body["invoice_lines"] == [{
        "label": "Abonnement Pro", "quantity": "-1", "unit": "piece",
        "raw_currency_unit_price": "100.00", "vat_rate": "FR_200",
    }]
    sent = repr(account.sent)
    assert "imputation_dates" not in sent
    assert "refund me" not in sent and "4242" not in sent


async def test_the_day_is_the_companys_own():
    account = credit_world("upcoming")
    provider = writing_provider(account)
    provider._now = lambda: datetime(2026, 11, 12, 23, 30, tzinfo=UTC)
    await credit(provider)
    assert account.sent[0][2]["date"] == "2026-11-13"


async def test_a_paid_invoice_is_credited_as_an_amount_owed():
    account = credit_world("paid")
    provider = writing_provider(account)
    assert await provider.plan_write(INVOICE, "credit_note") == "customer_balance"
    made = await credit(provider, allocation="customer_balance")
    assert (made.status, made.allocation) == ("succeeded", "customer_balance")
    assert account.document(600)["status"] == "paid"


async def test_an_untaxed_invoice_takes_one_line_at_its_no_tax_code():
    account = credit_world("upcoming", lines=[line(1, tax="0.00", code="exempt")])
    made = await credit(writing_provider(account), amount=4000, breakdown=None)
    assert account.sent[0][2]["invoice_lines"] == [{
        "label": "Avoir", "quantity": "-1", "unit": "piece",
        "raw_currency_unit_price": "40.00", "vat_rate": "exempt",
    }]
    assert (made.amount_minor, made.breakdown, made.allocation) == (4000, None, "reduce_due")


async def test_mixed_rates_an_exempt_line_and_a_small_price_are_reversed_line_by_line():
    lines = [line(1, "100.00", "20.00", "FR_200"), line(2, "10.00", "0.55", "FR_55"),
             line(3, "30.00", "0.00", "exempt"), line(4, "0.05", "0.01", "FR_200")]
    account = credit_world("late", lines=lines)
    breakdown = approved(14005, ((Decimal("5.5"), 55), (Decimal(20), 2001)), 16061)
    made = await credit(writing_provider(account), amount=16061, breakdown=breakdown)
    assert made.breakdown == breakdown
    assert [item["vat_rate"] for item in account.sent[0][2]["invoice_lines"]] == [
        "FR_200", "FR_55", "exempt", "FR_200"]
    assert all(item["quantity"] == "-1" for item in account.sent[0][2]["invoice_lines"])


@pytest.mark.parametrize(("status", "allocation"), [
    ("upcoming", "reduce_due"), ("late", "reduce_due"), ("paid", "customer_balance")])
async def test_plan_write_by_invoice_state(status, allocation):
    assert await writing_provider(credit_world(status)).plan_write(
        INVOICE, "credit_note") == allocation


@pytest.mark.parametrize("status", ["draft", "cancelled", "partially_cancelled",
                                    "partially_paid", "estimate_pending"])
async def test_plan_write_refuses_any_other_state(status):
    with pytest.raises(ProviderRejected, match="invoice_state"):
        await writing_provider(credit_world(status)).plan_write(INVOICE, "credit_note")


async def test_an_invoice_no_longer_in_its_planned_state_is_refused_before_anything_is_sent():
    account = credit_world("paid")
    with pytest.raises(ProviderRejected, match="invoice_state"):
        await credit(writing_provider(account), allocation="reduce_due")
    assert account.written() == []


async def test_an_invoice_that_changed_since_approval_is_refused_before_anything_is_sent():
    account = credit_world("upcoming")
    with pytest.raises(ProviderRejected, match="invoice_state"):
        await credit(writing_provider(account), amount=11000,
                     breakdown=approved(9000, ((Decimal(20), 2000),), 11000))
    assert account.written() == []


# -- the two cases that end as nothing written (FR-005) --------------------------------------


def _pennylane_rounds_differently(document, lines):
    lines[0]["currency_tax"] = "-19.99"
    lines[0]["currency_amount"] = "-119.99"
    document["currency_tax"] = "-19.99"
    document["currency_amount"] = "-119.99"


async def test_a_draft_unlike_the_approval_is_deleted_seen_gone_and_refused():
    account = credit_world("upcoming")
    account.draft_change = _pennylane_rounds_differently
    with pytest.raises(ProviderRejected) as refused:
        await credit(writing_provider(account))
    assert not isinstance(refused.value, PartialWrite)
    assert "draft_mismatch" in str(refused.value)
    assert "11999" in str(refused.value) and "12000" in str(refused.value)
    assert account.written() == [("POST", "/customer_invoices"),
                                 ("DELETE", f"/customer_invoices/{DRAFT}")]
    assert account.document(DRAFT) is None
    # Seen gone, by itself and from the customer's documents.
    assert account.calls[-2:] == [("GET", f"/customer_invoices/{DRAFT}"),
                                  ("GET", "/customer_invoices")]


@pytest.mark.parametrize("fault", [(422, "cannot delete"), "lost", "lost_before", "ack_only"])
async def test_a_deletion_not_seen_done_is_a_partial_write(fault):
    account = credit_world("upcoming")
    account.draft_change = _pennylane_rounds_differently
    account.faults["delete"] = fault
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert (partial.value.document_id, partial.value.stage) == (f"pl_inv_{DRAFT}", "deleting")


async def test_a_deletion_still_listed_is_a_partial_write():
    account = credit_world("upcoming")
    account.draft_change = _pennylane_rounds_differently
    original = account._delete

    def deleted_but_listed(id_):
        answer = original(id_)
        account.invoices.append({"id": 9999, "external_reference": REFERENCE,
                                 "customer": {"id": 7}})
        return answer

    account._delete = deleted_but_listed
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert partial.value.stage == "deleting"


@pytest.mark.parametrize("state", [
    {"draft": None}, {"draft": "yes"}, {"draft": True, "status": "cancelled"},
    {"draft": False, "status": "draft"}])
@pytest.mark.parametrize("path", ["mismatch", "cap"])
async def test_a_draft_whose_state_cannot_be_told_is_never_deleted(state, path):
    account = credit_world("upcoming")
    if path == "mismatch":
        def unclear(document, lines):
            _pennylane_rounds_differently(document, lines)
            document.update(state)
        account.draft_change = unclear
    else:
        account.faults["link"] = (400, CAP_REFUSAL)

        def unclear_after_link(method, request_path):
            if method == "POST" and request_path.endswith("link_credit_note"):
                account.document(DRAFT).update(state)

        account.on_request = unclear_after_link
    with pytest.raises(PartialWrite):
        await credit(writing_provider(account))
    assert ("DELETE", f"/customer_invoices/{DRAFT}") not in account.calls


@pytest.mark.parametrize("state", [{"draft": None}, {"draft": True, "status": "cancelled"}])
async def test_a_document_whose_state_cannot_be_told_is_never_taken_as_complete(state):
    account = credit_world("upcoming")
    provider = writing_provider(account)
    await credit(provider)
    account.document(DRAFT).update(state)
    with pytest.raises(ProviderUnavailable):
        await provider.find_action(KEY, INVOICE)
    account.document(600)["status"] = "upcoming"  # only the document's state differs
    with pytest.raises(ProviderRejected, match="partial_document"):
        await credit(provider, key="1a" * 32)


def _credited_elsewhere(account, amount="-60.00"):
    """A credit note made in Pennylane's interface, finalised, linked."""
    account.invoices.append({
        "id": 700, "customer": {"id": 7}, "status": "cancelled", "draft": False,
        "currency": "EUR", "currency_amount": amount, "credited_invoice": {"id": 600},
        "external_reference": None,
    })


async def test_a_link_refused_for_the_cap_deletes_the_draft_and_refuses():
    account = credit_world("upcoming")
    _credited_elsewhere(account)
    with pytest.raises(ProviderRejected) as refused:
        await credit(writing_provider(account))
    assert not isinstance(refused.value, PartialWrite)
    assert str(refused.value).startswith("cap")
    assert account.document(DRAFT) is None
    assert ("PUT", f"/customer_invoices/{DRAFT}/finalize") not in account.calls


async def test_a_cap_refusal_on_a_draft_no_longer_a_plain_draft_is_a_partial_write():
    account = credit_world("upcoming")
    account.faults["link"] = (400, CAP_REFUSAL)

    def linked_anyway(method, path):
        if method == "POST" and path.endswith("link_credit_note"):
            account.document(DRAFT)["credited_invoice"] = {"id": 600}

    account.on_request = linked_anyway
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert partial.value.stage == "linking"
    assert ("DELETE", f"/customer_invoices/{DRAFT}") not in account.calls


@pytest.mark.parametrize("fault", [(400, "Something else"), (422, CAP_REFUSAL), "lost",
                                   "lost_before", (500, "")])
async def test_any_other_answer_to_the_link_is_a_partial_write(fault):
    account = credit_world("upcoming")
    account.faults["link"] = fault
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert (partial.value.document_id, partial.value.stage) == (f"pl_inv_{DRAFT}", "linking")
    assert ("DELETE", f"/customer_invoices/{DRAFT}") not in account.calls


# -- lost, refused or unreadable answers (FR-006) -------------------------------------------


@pytest.mark.parametrize("fault", ["lost", "lost_before", "unreadable", (500, "")])
async def test_a_creation_whose_answer_is_lost_is_unknown_and_never_resent(fault):
    account = credit_world("upcoming")
    account.faults["create"] = fault
    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await credit(writing_provider(account))
    assert not isinstance(unknown.value, PartialWrite)
    assert unknown.value.category == "creation"
    assert account.written() == [("POST", "/customer_invoices")]


@pytest.mark.parametrize("answer", [[1, 2], "made", 42, {}, {"id": "9001"}, {"id": 0}])
async def test_a_creation_answered_with_no_document_is_unknown(answer):
    account = credit_world("upcoming")
    account.faults["create"] = ["answer", answer]
    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await credit(writing_provider(account))
    assert not isinstance(unknown.value, PartialWrite)
    assert unknown.value.category == "unreadable_answer"
    assert account.written() == [("POST", "/customer_invoices")]


async def test_a_creation_refused_wrote_nothing():
    account = credit_world("upcoming")
    account.faults["create"] = (400, "invalid line")
    with pytest.raises(ProviderRejected, match=r"refused \(400\)"):
        await credit(writing_provider(account))


async def test_a_creation_refused_for_its_reference_is_left_to_a_person():
    account = credit_world("upcoming")
    account.faults["create"] = (422, "External reference has already been taken")
    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await credit(writing_provider(account))
    assert unknown.value.category == "reference_taken"


@pytest.mark.parametrize("fault", ["lost", "lost_before", (422, "date"), (500, "")])
async def test_a_finalisation_not_answered_is_a_partial_write(fault):
    account = credit_world("upcoming")
    account.faults["finalize"] = fault
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert (partial.value.document_id, partial.value.stage) == (f"pl_inv_{DRAFT}", "finalising")


@pytest.mark.parametrize(("after", "path", "stage"), [
    ("POST /customer_invoices", f"/customer_invoices/{DRAFT}", "draft"),
    ("POST /customer_invoices", f"/customer_invoices/{DRAFT}/invoice_lines", "draft"),
    ("POST /customer_invoices/600/link_credit_note", "/customers/7", "linked"),
    (f"PUT /customer_invoices/{DRAFT}/finalize", f"/customer_invoices/{DRAFT}", "finalised"),
    (f"PUT /customer_invoices/{DRAFT}/finalize", "/customer_invoices/600", "finalised"),
])
async def test_a_read_that_fails_after_the_creation_is_a_partial_write(after, path, stage):
    account = credit_world("upcoming")

    def failing_after(method, request_path):
        if f"{method} {request_path}" == after:
            account.read_faults[path] = 503

    account.on_request = failing_after
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert (partial.value.document_id, partial.value.stage) == (f"pl_inv_{DRAFT}", stage)


async def test_a_read_that_fails_before_the_creation_sends_nothing():
    account = credit_world("upcoming")
    account.read_faults["/customers/7"] = 503
    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await credit(writing_provider(account))
    assert unknown.value.category == "invoice_read"
    assert account.written() == []


async def test_the_hook_refusing_before_the_creation_sends_nothing():
    account = credit_world("upcoming")
    with pytest.raises(NotSent):
        await credit(writing_provider(account), hook=Hook(fail_at=8))  # at the creation
    assert account.written() == []


@pytest.mark.parametrize("fail_at", [9, 11, 15, 16])
async def test_the_hook_refusing_after_the_creation_is_a_partial_write(fail_at):
    account = credit_world("upcoming")
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account), hook=Hook(fail_at=fail_at))
    assert partial.value.document_id == f"pl_inv_{DRAFT}"
    # Nothing was requested once the hook refused.
    assert len(account.calls) == fail_at - 1


async def test_the_time_budget_spent_after_the_creation_is_a_partial_write():
    account = credit_world("upcoming")
    provider = writing_provider(account)
    provider._write_budget = 0.05

    async def slow():
        if account.document(DRAFT) is not None:
            import asyncio

            await asyncio.sleep(1)

    with pytest.raises(PartialWrite) as partial:
        await credit(provider, hook=slow)
    assert partial.value.stage == "draft"


# -- refusals before anything is sent (FR-004, FR-013) -----------------------------------------


@pytest.mark.parametrize("change", [
    {"reg_no": "123456789"},
    {"vat_number": "FR12345678901"},
    {"electronic_billing_address": "0009:12345678900012"},
    {"e_invoicing_address": {"scheme": "0225"}},
    {"emails": ["someone@example.com"]},
    {"emails": []},
])
async def test_a_customer_that_could_receive_the_document_is_refused_before_creating(change):
    customer = {"id": 7, "name": "RB", "emails": [TEST_EMAIL], "reg_no": None,
                "vat_number": None, **change}
    account = credit_world("upcoming", customer=customer)
    with pytest.raises(ProviderRejected, match="outward"):
        await credit(writing_provider(account))
    assert account.written() == []


@pytest.mark.parametrize("change", ["customer", "document"])
async def test_a_document_that_could_go_out_is_never_finalised(change):
    account = credit_world("upcoming")

    def outward_after_link(method, path):
        if method == "POST" and path.endswith("link_credit_note"):
            if change == "customer":
                account.customers[0]["reg_no"] = "123456789"
            else:
                account.document(DRAFT)["e_invoicing"] = {"status": "queued"}

    account.on_request = outward_after_link
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert partial.value.stage == "linked"
    assert ("PUT", f"/customer_invoices/{DRAFT}/finalize") not in account.calls


@pytest.mark.parametrize("document", [
    {"draft": True, "status": "draft", "credited_invoice": None},
    {"draft": False, "status": "cancelled", "credited_invoice": None},
    {"draft": True, "status": "draft", "credited_invoice": {"id": 600}},
])
async def test_a_credit_note_of_ours_left_half_way_refuses_the_next(document):
    account = credit_world("upcoming")
    account.invoices.append({"id": 800, "customer": {"id": 7}, "currency": "EUR",
                             "external_reference": "rb:" + "ab" * 32, **document})
    with pytest.raises(ProviderRejected, match="partial_document"):
        await credit(writing_provider(account))
    assert account.written() == []


async def test_a_world_reference_is_not_taken_for_ours():
    account = credit_world("upcoming")
    account.document(600)["external_reference"] = "rb:taxed_invoice:invoice"
    made = await credit(writing_provider(account))
    assert made.status == "succeeded"


async def test_a_document_already_under_the_key_is_never_doubled():
    account = credit_world("upcoming")
    account.invoices.append({"id": 800, "customer": {"id": 7}, "draft": False,
                             "status": "cancelled", "credited_invoice": {"id": 600},
                             "external_reference": REFERENCE})
    with pytest.raises(ProviderOutcomeUnknown) as unknown:
        await credit(writing_provider(account))
    assert unknown.value.category == "reference_taken"
    assert account.written() == []


@pytest.mark.parametrize("key", ["ab" * 200, "short", "AB" * 32, ""])
async def test_a_key_that_cannot_be_the_reference_is_refused_before_anything(key):
    account = credit_world("upcoming")
    with pytest.raises(ProviderRejected, match="reference"):
        await credit(writing_provider(account), key=key)
    assert account.calls == []


async def test_the_reference_limit_is_checked_before_anything(monkeypatch):
    monkeypatch.setattr(mapping, "REFERENCE_MAX", 60)
    account = credit_world("upcoming")
    with pytest.raises(ProviderRejected, match="reference"):
        await credit(writing_provider(account))
    assert account.calls == []


async def test_an_allocation_of_another_type_is_refused_before_anything():
    account = credit_world("paid")
    with pytest.raises(ProviderRejected, match="allocation"):
        await credit(writing_provider(account), allocation="refund")
    assert account.calls == []


# -- writes off, another company, refunds (FR-001, FR-002) -------------------------------------


async def test_without_a_writer_nothing_is_written_and_reads_still_work():
    account = credit_world("upcoming")
    provider = writing_provider(account, writes=False)
    assert provider.writes == frozenset() and provider.can_write is False
    with pytest.raises(PlatformCannotWrite):
        await provider.plan_write(INVOICE, "credit_note")
    with pytest.raises(PlatformCannotWrite):
        await credit(provider)
    assert account.calls == []
    assert await provider.find_action(KEY, INVOICE) == []


async def test_a_writer_writes_credit_notes_only():
    provider = writing_provider(credit_world("paid"))
    assert provider.writes == frozenset({"credit_note"}) and provider.can_write is True
    assert provider.idempotency_window is None
    with pytest.raises(PlatformCannotWrite):
        await provider.plan_write(INVOICE, "refund")
    with pytest.raises(PlatformCannotWrite):
        await provider.create_refund(INVOICE, 12000, "EUR", "r", KEY, action_id="a",
                                     allocation="refund", send_by=None)


async def test_a_write_token_of_another_company_writes_nothing():
    account = credit_world("upcoming")
    account.tokens["other-token"] = 1
    provider = writing_provider(account, write_token="other-token")
    with pytest.raises(ProviderRejected, match="sandbox"):
        await provider.plan_write(INVOICE, "credit_note")
    assert provider.writes == frozenset() and provider.can_write is False
    with pytest.raises(PlatformCannotWrite):
        await credit(provider)
    assert account.written() == []


async def test_a_write_token_of_another_company_refuses_the_write_itself():
    account = credit_world("upcoming")
    account.tokens["other-token"] = 1
    with pytest.raises(ProviderRejected, match="sandbox"):
        await credit(writing_provider(account, write_token="other-token"))
    assert account.written() == []


async def test_the_writer_never_requests_an_email_or_e_invoicing_path():
    provider = writing_provider(credit_world("upcoming"))
    for path in ("/customer_invoices/1/send_by_email", "/customer_invoices/e_invoices/imports"):
        with pytest.raises(ValueError):
            await provider._writer.write("POST", path, {})


# -- find_action (FR-008, FR-013) ---------------------------------------------------------------


async def test_find_action_finds_the_complete_credit_note_alike():
    account = credit_world("upcoming")
    provider = writing_provider(account)
    await credit(provider)
    (found,) = await provider.find_action(KEY, INVOICE)
    assert (found.status, found.partial, found.allocation) == ("succeeded", None, "reduce_due")
    assert found.breakdown == approved()
    assert (found.invoice_id, found.amount_minor, found.test_mode) == (INVOICE, 12000, True)
    no_outward_path(account)


async def test_find_action_needs_no_writer():
    account = credit_world("upcoming")
    await credit(writing_provider(account))
    (found,) = await writing_provider(account, writes=False).find_action(KEY, INVOICE)
    assert found.partial is None


@pytest.mark.parametrize(("fault", "partial"), [("link", "draft"), ("finalize", "draft")])
async def test_find_action_reports_a_document_left_half_way(fault, partial):
    account = credit_world("upcoming")
    account.faults[fault] = "lost_before"
    provider = writing_provider(account)
    with pytest.raises(PartialWrite):
        await credit(provider)
    (found,) = await provider.find_action(KEY, INVOICE)
    assert found.partial == partial
    assert found.status is None and found.allocation is None


async def test_find_action_reports_a_finalised_unlinked_document():
    account = credit_world("upcoming")
    account.invoices.append({
        "id": 800, "customer": {"id": 7}, "draft": False, "status": "cancelled",
        "credited_invoice": None, "external_reference": REFERENCE, "currency": "EUR",
        "currency_amount": "-120.00", "currency_amount_before_tax": "-100.00",
        "currency_tax": "-20.00", "date": "2026-11-12",
    })
    account.lines[800] = [line(1, "-100.00", "-20.00")]
    (found,) = await writing_provider(account).find_action(KEY, INVOICE)
    assert (found.partial, found.invoice_id) == ("unlinked", "")


async def test_a_creation_that_lands_after_a_read_showed_nothing_is_found_later():
    account = credit_world("upcoming")
    account.faults["create"] = "lost_before"
    provider = writing_provider(account)
    with pytest.raises(ProviderOutcomeUnknown):
        await credit(provider)
    assert await provider.find_action(KEY, INVOICE) == []
    # The request was in flight: it lands now.
    (_, _, body), = [s for s in account.sent if s[1] == "/customer_invoices"]
    account._create(body)
    (found,) = await provider.find_action(KEY, INVOICE)
    assert found.partial == "draft"


async def test_find_action_returns_several_and_unlike_as_they_are():
    account = credit_world("upcoming")
    provider = writing_provider(account)
    await credit(provider)
    document = dict(account.document(DRAFT), id=801, currency_amount="-60.00",
                    currency_amount_before_tax="-40.00")
    account.invoices.append(document)
    account.lines[801] = [line(1, "-40.00", "-20.00")]
    found = await provider.find_action(KEY, INVOICE)
    assert len(found) == 2
    assert {f.amount_minor for f in found} == {12000, 6000}


async def test_find_action_finds_nothing_under_another_key():
    account = credit_world("upcoming")
    provider = writing_provider(account)
    await credit(provider)
    assert await provider.find_action("1a" * 32, INVOICE) == []


async def test_a_document_under_the_key_that_cannot_be_read_is_never_an_absence():
    account = credit_world("upcoming")
    account.invoices.append({"id": 800, "customer": {"id": 7}, "draft": False,
                             "status": "cancelled", "credited_invoice": {"id": 600},
                             "external_reference": REFERENCE, "currency": "EUR",
                             "currency_amount": "oops"})
    with pytest.raises(ProviderUnavailable):
        await writing_provider(account).find_action(KEY, INVOICE)


# -- the mapper's pieces --------------------------------------------------------------------


class _Refusal:
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


@pytest.mark.parametrize(("status", "detail", "cap"), [
    (400, CAP_REFUSAL, True),
    (400, "The credit notes amount is bigger than the invoice amount", True),
    (400, "Something else", False),
    (422, CAP_REFUSAL, False),
])
def test_is_cap_refusal(status, detail, cap):
    assert mapping.is_cap_refusal(_Refusal(status, detail)) is cap


def test_an_all_zero_tax_breakdown_is_none():
    document = {"currency": "EUR", "currency_amount": "-40.00",
                "currency_amount_before_tax": "-40.00", "currency_tax": "0.00"}
    lines = [{"currency_tax": "0.00", "vat_rate": "exempt"},
             {"currency_tax": "0.00", "vat_rate": "FR_200"}]
    assert mapping.observed_breakdown(document, lines) is None


def test_the_observed_breakdown_is_positive_and_by_rate():
    document = {"currency": "EUR", "currency_amount": "-120.55",
                "currency_amount_before_tax": "-110.00", "currency_tax": "-10.55"}
    lines = [{"currency_tax": "-10.00", "vat_rate": "FR_200"},
             {"currency_tax": "-0.55", "vat_rate": "FR_55"}]
    assert mapping.observed_breakdown(document, lines) == approved(
        11000, ((Decimal("5.5"), 55), (Decimal(20), 1000)), 12055)


def test_an_observed_breakdown_whose_lines_do_not_make_the_tax_is_refused():
    document = {"currency": "EUR", "currency_amount": "-120.00",
                "currency_amount_before_tax": "-100.00", "currency_tax": "-20.00"}
    with pytest.raises(Exception, match="tax_mismatch"):
        mapping.observed_breakdown(document, [{"currency_tax": "-19.99",
                                               "vat_rate": "FR_200"}])


@pytest.mark.parametrize(("addresses", "ready"), [
    ([TEST_EMAIL], True),
    (["a@x.test", "b@y.test"], True),
    (["a@x.test", "b@example.com"], False),
    (["a@test"], False),
    (["not-an-address.test"], False),
    ([], False),
])
def test_outward_ready_addresses(addresses, ready):
    assert mapping.outward_ready({"reg_no": None, "vat_number": ""}, addresses) is ready


def test_wiring_builds_the_writer_only_when_enabled(monkeypatch):
    from app.api import deps
    from app.providers.pennylane.keys import ProviderConfigurationError

    monkeypatch.setenv("BILLING_PROVIDER", "pennylane")
    monkeypatch.setenv("PENNYLANE_API_TOKEN", "read-token")
    monkeypatch.setenv("PENNYLANE_SANDBOX_COMPANY_ID", "282871")
    monkeypatch.delenv("PENNYLANE_WRITE_TOKEN", raising=False)
    for value, writes in ((None, frozenset()), ("yes", frozenset()),
                          ("enabled", frozenset({"credit_note"}))):
        if value is None:
            monkeypatch.delenv("PENNYLANE_WRITES", raising=False)
        else:
            monkeypatch.setenv("PENNYLANE_WRITES", value)
            monkeypatch.setenv("PENNYLANE_WRITE_TOKEN", "write-token")
        deps.get_provider.cache_clear()
        try:
            provider = deps.get_provider()
        finally:
            deps.get_provider.cache_clear()
        assert isinstance(provider, PennylaneBillingProvider)
        assert provider.writes == writes
    monkeypatch.delenv("PENNYLANE_WRITE_TOKEN")
    with pytest.raises(ProviderConfigurationError) as raised:
        deps.get_provider()
    deps.get_provider.cache_clear()
    assert raised.value.category == "missing_token"


@pytest.mark.parametrize("stage", ["before creating", "before finalising"])
async def test_an_eligible_electronic_billing_address_stops_the_write(stage):
    """FR-004: Pennylane listing a selectable e-invoicing address for the
    customer means a document could be routed out: refused before creating,
    a partial write before finalising -- never finalised."""
    account = credit_world("upcoming")
    if stage == "before creating":
        account.eligible = ["123456789"]
        with pytest.raises(ProviderRejected):
            await credit(writing_provider(account))
        assert account.written() == []
        return

    def appear(method, path):
        if method == "POST" and path.endswith("/link_credit_note"):
            account.eligible = ["123456789"]

    account.on_request = appear
    with pytest.raises(PartialWrite) as partial:
        await credit(writing_provider(account))
    assert partial.value.stage == "linked"
    assert not any(path.endswith("/finalize") for _, path, _ in account.sent)


def test_a_draft_credit_note_as_pennylane_answers_it_is_a_draft():
    """Seen on the sandbox's first write run (2026-11-13): a draft credit note
    reads `draft: true`, status `credit_note`, an empty number. Taken for
    unreadable, every credit note stopped half-way, left to a person."""
    from app.providers.pennylane import mapping

    assert mapping.is_draft({"draft": True, "status": "credit_note", "invoice_number": ""})
    assert mapping.partial_stage({"draft": True, "status": "credit_note",
                                  "credited_invoice": None}) == "draft"
    assert not mapping.is_draft({"draft": False, "status": "cancelled"})

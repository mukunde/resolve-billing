"""The one path by which money moves: what may and may not become EXECUTED."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.application.execute_action import ApprovalService
from app.domain.enums import ActionState, InvoiceStatus
from app.domain.money import Money
from app.infrastructure.actions import IllegalTransition, NeedsAPerson, SqliteActionStore
from app.providers.base import ProviderOutcomeUnknown, ProviderRejected, ProviderUnavailable
from app.providers.fake.adapter import FakeBillingProvider

NINETY_NINE = Money(amount_minor=9900, currency="EUR")


class FlakyProvider(FakeBillingProvider):
    """Refuses the first `failures` writes, then behaves like the fake."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.calls = 0

    async def create_credit_note(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderRejected("provider refused the write")
        return await super().create_credit_note(**kwargs)


@pytest.fixture
def store():
    actions = SqliteActionStore(":memory:")
    yield actions
    actions.close()


async def written_behind(provider: FakeBillingProvider, action) -> None:
    """The write landed on the platform, as a process that died before
    recording it would have left it."""
    await provider.create_credit_note(
        invoice_id=action.invoice_id,
        amount_minor=action.amount.amount_minor,
        currency=action.amount.currency,
        reason=action.reason,
        idempotency_key=action.idempotency_key,
        action_id=action.action_id,
        allocation=await provider.plan_write(action.invoice_id, action.action_type),
        send_by=datetime.now(UTC) + timedelta(hours=1),
    )


def approved(store: SqliteActionStore):
    action = store.create(
        run_id="run_test",
        action_type="credit_note",
        invoice_id="inv_002",
        amount=NINETY_NINE,
        reason="duplicate invoice",
        provider="fake",
    )
    return store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")


class TestNothingIsExecutedWithoutTheProvider:
    """D1. A failed action could be marked EXECUTED by a second call to
    execute(), with no write on the billing platform: the state table allowed
    FAILED -> EXECUTED, and execute() went straight to the transition for any
    state other than APPROVED."""

    async def test_a_retry_after_a_failure_calls_the_provider_again(self, store):
        provider = FlakyProvider(failures=1)
        service = ApprovalService(provider, store)
        action = approved(store)

        with pytest.raises(ProviderRejected):
            await service.execute(action.action_id)
        assert store.get(action.action_id).state is ActionState.FAILED

        done = await service.execute(action.action_id)

        assert provider.calls == 2
        assert done.state is ActionState.EXECUTED
        assert done.provider_action_id is not None
        assert len(provider.executed_actions) == 1

    async def test_a_retry_that_fails_again_stays_failed(self, store):
        provider = FlakyProvider(failures=2)
        service = ApprovalService(provider, store)
        action = approved(store)

        for _ in range(2):
            with pytest.raises(ProviderRejected):
                await service.execute(action.action_id)

        assert store.get(action.action_id).state is ActionState.FAILED
        assert provider.calls == 2
        assert provider.executed_actions == []

    def test_the_store_refuses_executed_without_a_provider_reference(self, store):
        """The guard lives in the store, so no future caller can skip it."""
        action = approved(store)

        with pytest.raises(IllegalTransition):
            store.transition(action.action_id, ActionState.EXECUTED)

        assert store.get(action.action_id).state is ActionState.APPROVED

    async def test_an_action_awaiting_approval_is_still_refused(self, store):
        service = ApprovalService(FakeBillingProvider(), store)
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=NINETY_NINE,
            reason="duplicate invoice",
            provider="fake",
        )

        with pytest.raises(IllegalTransition):
            await service.execute(action.action_id)


class SlowProvider(FakeBillingProvider):
    """Yields to the event loop mid-write, so two executions can interleave."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def create_credit_note(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.01)
        return await super().create_credit_note(**kwargs)


class TestOneExecutionAtATime:
    """D2. Two executions of one approved action both read APPROVED and both
    called the provider: the check was made in Python, and the UPDATE that
    followed named the action but not the state it expected to find."""

    async def test_two_concurrent_executions_call_the_provider_once(self, store):
        provider = SlowProvider()
        service = ApprovalService(provider, store)
        action = approved(store)

        outcomes = await asyncio.gather(
            service.execute(action.action_id),
            service.execute(action.action_id),
            return_exceptions=True,
        )

        assert provider.calls == 1
        assert len(provider.executed_actions) == 1
        assert store.get(action.action_id).state is ActionState.EXECUTED
        refused = [o for o in outcomes if isinstance(o, Exception)]
        assert len(refused) == 1 and isinstance(refused[0], IllegalTransition)

    def test_an_action_being_executed_cannot_be_claimed_again(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING)

        with pytest.raises(IllegalTransition):
            store.transition(action.action_id, ActionState.EXECUTING)

    def test_the_update_refuses_a_state_that_moved_since_it_was_read(self, store, monkeypatch):
        """Another process got there between the read and the write: the
        UPDATE names the state it expects, and finds nothing to change."""
        action = approved(store)
        stale = store.get(action.action_id)
        store.transition(action.action_id, ActionState.EXECUTING)
        monkeypatch.setattr(store, "get", lambda action_id: stale)

        with pytest.raises(IllegalTransition):
            store.transition(action.action_id, ActionState.EXECUTING)


class LostResponseProvider(FakeBillingProvider):
    """The write lands on the platform, then the response never arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.lose_responses = True

    async def create_credit_note(self, **kwargs):
        self.writes += 1
        action = await super().create_credit_note(**kwargs)
        if self.lose_responses:
            raise TimeoutError("no response from the billing platform")
        return action


class TestATimeoutIsNotAFailure:
    """D3. Every exception was recorded as FAILED, a timeout included. In
    finance a timeout is not a failure: the credit note may well exist on
    the platform with its response lost on the way back."""

    async def test_no_answer_is_recorded_as_an_unknown_outcome(self, store):
        provider = LostResponseProvider()
        action = approved(store)

        with pytest.raises(TimeoutError):
            await ApprovalService(provider, store).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert "no response" in held.failure_reason

    async def test_an_explicit_refusal_is_still_a_failure(self, store):
        action = approved(store)

        with pytest.raises(ProviderRejected):
            await ApprovalService(FlakyProvider(failures=1), store).execute(action.action_id)

        assert store.get(action.action_id).state is ActionState.FAILED

    async def test_a_lost_response_is_recovered_once_across_a_restart(self, tmp_path):
        """The scenario the review asked for before any pilot: the provider
        creates the credit note, the response is lost, the service restarts,
        and the retry is requested twice."""
        database = tmp_path / "actions.db"
        provider = LostResponseProvider()
        before = SqliteActionStore(database)
        action = approved(before)
        with pytest.raises(TimeoutError):
            await ApprovalService(provider, before).execute(action.action_id)
        before.close()

        provider.lose_responses = False
        after = SqliteActionStore(database)
        service = ApprovalService(provider, after)
        first = await service.execute(action.action_id)
        second = await service.execute(action.action_id)

        assert provider.writes == 1, "the retry found the credit note instead of issuing it"
        assert len(provider.executed_actions) == 1
        assert first.state is second.state is ActionState.EXECUTED
        assert first.provider_action_id == provider.executed_actions[0].provider_action_id
        assert "reconciled" in first.resolution
        after.close()

    async def test_reconciliation_retries_when_nothing_was_written(self, store):
        """No answer, and the platform holds nothing under the key: the
        write never landed, so it is made again -- once."""

        class NeverArrives(FlakyProvider):
            async def create_credit_note(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("connection dropped before the write")
                return await FakeBillingProvider.create_credit_note(self, **kwargs)

        provider = NeverArrives(failures=0)
        service = ApprovalService(provider, store)
        action = approved(store)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)

        done = await service.execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert provider.calls == 2
        assert len(provider.executed_actions) == 1

    async def test_an_action_left_executing_by_a_crash_is_reconciled(self, store):
        """A process died between claiming the write and recording its
        result. Past a grace period, the claim is treated as an unknown
        outcome and resolved against the platform."""
        provider = FakeBillingProvider()
        action = approved(store)
        store.transition(
            action.action_id, ActionState.EXECUTING, first_attempt_at=datetime.now(UTC)
        )
        store.annotate(action.action_id, ActionState.EXECUTING, allocation="customer_balance")
        await written_behind(provider, action)

        service = ApprovalService(provider, store)
        assert (await service.reconcile(action.action_id)).state is ActionState.EXECUTING

        resolved = await service.reconcile(action.action_id, stale_after=timedelta(0))

        assert resolved.state is ActionState.EXECUTED
        assert len(provider.executed_actions) == 1

    async def test_execute_recovers_an_abandoned_claim_on_its_own(self, store):
        """No separate endpoint is needed: the next execute finds the claim
        abandoned, asks the platform, and records what it holds."""
        provider = FakeBillingProvider()
        action = approved(store)
        store.transition(
            action.action_id, ActionState.EXECUTING, first_attempt_at=datetime.now(UTC)
        )
        store.annotate(action.action_id, ActionState.EXECUTING, allocation="customer_balance")
        await written_behind(provider, action)

        fresh = ApprovalService(provider, store)
        with pytest.raises(IllegalTransition):
            await fresh.execute(action.action_id)

        late = ApprovalService(provider, store, stale_after=timedelta(0))
        done = await late.execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert len(provider.executed_actions) == 1

    async def test_two_writes_under_the_key_leave_the_outcome_unknown(self, store):
        """find_action answers every write under the key: two is an incident
        for a person, never "the first one found" (specs/002-stripe-writes,
        FR-001, FR-004)."""
        provider = LostResponseProvider()
        action = approved(store)
        with pytest.raises(TimeoutError):
            await ApprovalService(provider, store).execute(action.action_id)
        (made,) = provider.executed_actions
        provider.plant_action(made.model_copy(update={"provider_action_id": "cn_other"}))
        provider.lose_responses = False

        held = await ApprovalService(provider, store).reconcile(action.action_id)

        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "several_writes"
        assert provider.writes == 1


def approved_refund(store: SqliteActionStore):
    action = store.create(
        run_id="run_test",
        action_type="refund",
        invoice_id="inv_002",
        amount=NINETY_NINE,
        reason="charged twice",
        provider="fake",
    )
    return store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")


class TestARefundIsARefund:
    """D4. execute() only knew create_credit_note, so an approved refund was
    issued as a credit note: the human approved one instrument and the
    platform received another."""

    async def test_an_approved_refund_is_executed_as_a_refund(self, store):
        provider = FakeBillingProvider()
        action = approved_refund(store)

        done = await ApprovalService(provider, store).execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        (written,) = provider.executed_actions
        assert written.action_type == "refund"

    async def test_a_refund_with_no_settled_payment_is_refused(self, store):
        """inv_020 has no settled payment: there is nothing to refund, and
        the platform says so instead of issuing a credit note."""
        provider = FakeBillingProvider()
        action = store.create(
            run_id="run_test",
            action_type="refund",
            invoice_id="inv_020",
            amount=NINETY_NINE,
            reason="proration",
            provider="fake",
        )
        store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")

        with pytest.raises(ProviderRejected):
            await ApprovalService(provider, store).execute(action.action_id)

        assert provider.executed_actions == []
        assert store.get(action.action_id).state is ActionState.FAILED

    async def test_an_action_type_the_platform_cannot_write_is_refused_before_the_claim(
        self, store
    ):
        provider = FakeBillingProvider()
        action = store.create(
            run_id="run_test",
            action_type="write_off",
            invoice_id="inv_002",
            amount=NINETY_NINE,
            reason="x",
            provider="fake",
        )
        store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")

        with pytest.raises(IllegalTransition):
            await ApprovalService(provider, store).execute(action.action_id)

        assert store.get(action.action_id).state is ActionState.APPROVED
        assert provider.executed_actions == []


class TestNeverMoreThanTheInvoice:
    """D6, on our side. The platform holds the ledger and caps corrections;
    ResolveBilling keeps one minimal guard of its own, so a bug in a proposal
    does not rely on the platform alone to stop it."""

    async def test_an_amount_above_the_invoice_total_is_refused_before_the_claim(self, store):
        provider = FakeBillingProvider()
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=Money(amount_minor=12000, currency="EUR"),
            reason="x",
            provider="fake",
        )
        store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")

        with pytest.raises(IllegalTransition):
            await ApprovalService(provider, store).execute(action.action_id)

        assert store.get(action.action_id).state is ActionState.APPROVED
        assert provider.executed_actions == []

    async def test_a_refund_after_a_full_credit_note_fails_at_the_platform(self, store):
        """The 198 EUR case end to end: both approved, the second refused by
        the platform, recorded as FAILED with the platform's reason."""
        provider = FakeBillingProvider()
        service = ApprovalService(provider, store)
        credit = approved(store)
        await service.execute(credit.action_id)
        refund = approved_refund(store)

        with pytest.raises(ProviderRejected):
            await service.execute(refund.action_id)

        assert store.get(refund.action_id).state is ActionState.FAILED
        assert len(provider.executed_actions) == 1


class TestAnApprovalHoldsForWhatWasShownAndNotForever:
    """D13. An approval request stayed valid indefinitely, and nothing tied
    the decision to the proposal the approver had seen: if the invoice
    changed in between, the approval still applied."""

    async def shown(self, service, action):
        return await service.proposal_fingerprint(action)

    async def test_an_approval_of_what_was_shown_goes_through(self, store):
        service = ApprovalService(FakeBillingProvider(), store)
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=NINETY_NINE,
            reason="x",
            provider="fake",
        )

        shown = await self.shown(service, action)
        done = await service.approve(action.action_id, "approver", shown)

        assert done.state is ActionState.APPROVED
        assert done.approved_fingerprint == await self.shown(service, action)

    async def test_a_request_past_its_deadline_cannot_be_decided(self, store):
        service = ApprovalService(FakeBillingProvider(), store)
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=NINETY_NINE,
            reason="x",
            provider="fake",
            ttl=timedelta(seconds=-1),
        )

        with pytest.raises(IllegalTransition):
            await service.approve(action.action_id, "approver", await self.shown(service, action))

        assert store.get(action.action_id).state is ActionState.EXPIRED

    async def test_an_approval_of_a_proposal_that_changed_since_it_was_shown_falls(self, store):
        provider = FakeBillingProvider()
        service = ApprovalService(provider, store)
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=NINETY_NINE,
            reason="x",
            provider="fake",
        )
        shown = await self.shown(service, action)
        provider.change_invoice("inv_002", total_minor=4900)

        with pytest.raises(IllegalTransition):
            await service.approve(action.action_id, "approver", shown)

        held = store.get(action.action_id)
        assert held.state is ActionState.EXPIRED
        assert "changed" in held.resolution

    async def test_an_invoice_that_changed_after_approval_is_not_written(self, store):
        provider = FakeBillingProvider()
        service = ApprovalService(provider, store)
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=NINETY_NINE,
            reason="x",
            provider="fake",
        )
        await service.approve(action.action_id, "approver", await self.shown(service, action))
        provider.change_invoice("inv_002", total_minor=4900)

        with pytest.raises(IllegalTransition):
            await service.execute(action.action_id)

        assert store.get(action.action_id).state is ActionState.EXPIRED
        assert provider.executed_actions == []


class ReadOnlyProvider(FakeBillingProvider):
    """A platform that cannot write: every call is recorded, and any read,
    write or reconciliation fails the test (specs/001-stripe-read-adapter,
    FR-003)."""

    can_write = False

    def __init__(self) -> None:
        super().__init__()
        self.touched: list[str] = []

    async def get_invoice(self, invoice_id):
        self.touched.append("get_invoice")
        raise AssertionError("no read before refusing")

    async def create_credit_note(self, **kwargs):
        self.touched.append("create_credit_note")
        raise AssertionError("no write on a read-only platform")

    async def find_action(self, idempotency_key, invoice_id):
        self.touched.append("find_action")
        raise AssertionError("no reconciliation on a read-only platform")


class TestAReadOnlyPlatformIsNeverWritten:
    async def test_execute_is_refused_before_anything(self, store):
        from app.infrastructure.actions import WritesNotSupported

        provider = ReadOnlyProvider()
        action = approved(store)

        with pytest.raises(WritesNotSupported):
            await ApprovalService(provider, store).execute(action.action_id)

        assert provider.touched == []
        assert store.get(action.action_id).state is ActionState.APPROVED

    async def test_it_is_a_transition_refusal_so_the_api_answers_409(self):
        from app.infrastructure.actions import WritesNotSupported

        assert issubclass(WritesNotSupported, IllegalTransition)

    async def test_reconcile_never_asks_the_platform(self, store):
        from app.infrastructure.actions import WritesNotSupported

        provider = ReadOnlyProvider()
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME)

        with pytest.raises(WritesNotSupported):
            await ApprovalService(provider, store).reconcile(action.action_id)
        assert provider.touched == []
        assert store.get(action.action_id).state is ActionState.UNKNOWN_OUTCOME

    async def test_a_platform_that_cannot_write_says_so_without_a_call(self):
        from app.providers.base import PlatformCannotWrite

        assert issubclass(PlatformCannotWrite, Exception)
        assert not issubclass(PlatformCannotWrite, ProviderRejected)


class Answering(FakeBillingProvider):
    """Makes the write, then answers with something else: what a platform
    returning a malformed or mismatched answer looks like."""

    def __init__(self, **answer) -> None:
        super().__init__()
        self.answer = answer
        self.calls = 0

    async def create_credit_note(self, *args, **kwargs):
        self.calls += 1
        made = await super().create_credit_note(*args, **kwargs)
        return made.model_copy(update=self.answer)

    async def create_refund(self, *args, **kwargs):
        self.calls += 1
        made = await super().create_refund(*args, **kwargs)
        return made.model_copy(update=self.answer)


class TestTheAnswerIsCheckedBeforeExecuted:
    """specs/002-stripe-writes, FR-003 and FR-004: the platform's own answer
    is checked against the action before the action is EXECUTED."""

    async def test_an_answer_alike_records_the_write_and_its_status(self, store):
        provider = FakeBillingProvider()
        action = approved(store)

        done = await ApprovalService(provider, store).execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert done.provider_status == "succeeded"
        assert done.provider_status_at is not None
        assert done.allocation == "customer_balance"
        assert done.attention is None

    async def test_a_status_but_success_is_executed_and_put_to_a_person(self, store):
        provider = Answering(status="pending")
        action = approved_refund(store)

        done = await ApprovalService(provider, store).execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert done.provider_status == "pending"
        assert done.attention == "not_successful"
        assert "pending" in done.resolution
        assert await ApprovalService(provider, store).execute(action.action_id) == done
        assert provider.calls == 1

    @pytest.mark.parametrize(
        "answer",
        [
            {"invoice_id": "inv_001"},
            {"action_type": "refund"},
            {"amount_minor": 9800},
            {"currency": "USD"},
            {"test_mode": False},
            {"allocation": "reduce_due"},
            {"idempotency_key": "another"},
            {"action_id": "act_another"},
        ],
        ids=lambda answer: next(iter(answer)),
    )
    async def test_an_answer_unlike_the_action_is_an_incident(self, store, answer):
        provider = Answering(**answer)
        action = approved(store)
        service = ApprovalService(provider, store)

        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "unlike_the_action"
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        assert provider.calls == 1

    @pytest.mark.parametrize("missing", ["allocation", "status"])
    async def test_an_answer_that_does_not_say_what_it_made_is_unreadable(self, store, missing):
        provider = Answering(**{missing: None})
        action = approved(store)

        with pytest.raises(NeedsAPerson):
            await ApprovalService(provider, store).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "unreadable_answer"

    async def test_an_answer_the_adapter_could_not_read_is_unreadable(self, store):
        class Unreadable(FakeBillingProvider):
            async def create_credit_note(self, *args, **kwargs):
                await super().create_credit_note(*args, **kwargs)
                raise ProviderOutcomeUnknown("unreadable_answer")

        action = approved(store)

        with pytest.raises(ProviderOutcomeUnknown):
            await ApprovalService(Unreadable(), store).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "unreadable_answer"


class Recording(FakeBillingProvider):
    """Records every send; the first `lost` sends never arrive."""

    def __init__(self, lost: int = 0) -> None:
        super().__init__()
        self.sent: list[dict] = []
        self.lost = lost

    async def create_credit_note(self, *args, **kwargs):
        self.sent.append(kwargs)
        if len(self.sent) <= self.lost:
            raise TimeoutError("connection dropped before the write")
        return await super().create_credit_note(*args, **kwargs)


class TestTheParametersAreFixedOnce:
    """specs/002-stripe-writes, FR-005 and plan section 3: how a write lands
    is planned once, before its first send, and every retry sends exactly
    that."""

    async def test_two_sends_of_one_action_are_the_same_request_across_a_restart(self, tmp_path):
        database = tmp_path / "actions.db"
        provider = Recording(lost=1)
        before = SqliteActionStore(database)
        action = approved(before)
        with pytest.raises(TimeoutError):
            await ApprovalService(provider, before).execute(action.action_id)
        before.close()

        after = SqliteActionStore(database)
        done = await ApprovalService(provider, after).execute(action.action_id)
        after.close()

        assert done.state is ActionState.EXECUTED
        first, second = provider.sent
        assert first == second

    async def test_an_invoice_paid_between_the_plan_and_a_retry_is_not_written_otherwise(
        self, store
    ):
        provider = Recording(lost=1)
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_020",
            amount=NINETY_NINE,
            reason="x",
            provider="fake",
        )
        store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")
        service = ApprovalService(provider, store)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        provider.change_invoice("inv_020", status=InvoiceStatus.PAID)

        with pytest.raises(ProviderRejected):
            await service.execute(action.action_id)

        assert {sent["allocation"] for sent in provider.sent} == {"reduce_due"}
        assert provider.executed_actions == []
        assert store.get(action.action_id).state is ActionState.FAILED

    async def test_a_plan_that_cannot_be_read_is_an_unknown_outcome(self, store):
        class Unreachable(FakeBillingProvider):
            async def plan_write(self, invoice_id, action_type):
                raise ProviderUnavailable("the platform did not answer")

        action = approved(store)

        with pytest.raises(ProviderUnavailable):
            await ApprovalService(Unreachable(), store).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.allocation is None

    async def test_an_action_in_another_currency_is_refused_before_the_claim(self, store):
        provider = FakeBillingProvider()
        action = store.create(
            run_id="run_test",
            action_type="credit_note",
            invoice_id="inv_002",
            amount=Money(amount_minor=9900, currency="USD"),
            reason="x",
            provider="fake",
        )
        store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")

        with pytest.raises(IllegalTransition):
            await ApprovalService(provider, store).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.APPROVED
        assert held.first_attempt_at is None
        assert provider.executed_actions == []


async def left_to_a_person(
    store, provider, state, attention, *, written=True, allocation="customer_balance"
):
    """An action an incident left to a person, its write landed or not."""
    action = approved(store)
    now = datetime.now(UTC)
    store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=now)
    if allocation is not None:
        store.annotate(action.action_id, ActionState.EXECUTING, allocation=allocation)
    if written:
        await written_behind(provider, action)
    if state is ActionState.UNKNOWN_OUTCOME:
        store.transition(action.action_id, state, uncertain_since=now)
    elif state is ActionState.FAILED:
        store.transition(action.action_id, state)
    return store.annotate(action.action_id, state, attention=attention)


class TestAPersonReadsTheStatus:
    """specs/002-stripe-writes, FR-003: the status recorded is an
    observation; a person can have it read again, which writes nothing."""

    async def test_a_status_read_again_is_a_new_observation(self, store):
        provider = FakeBillingProvider()
        service = ApprovalService(provider, store)
        action = approved_refund(store)
        done = await service.execute(action.action_id)
        provider.set_status(done.provider_action_id, "failed")

        read = await service.refresh_status(action.action_id)

        assert read.state is ActionState.EXECUTED
        assert read.provider_status == "failed"
        assert read.provider_status_at >= done.provider_status_at
        assert read.attention == "not_successful"
        assert len(provider.executed_actions) == 1

    async def test_success_seen_later_clears_the_follow_up(self, store):
        provider = Answering(status="pending")
        service = ApprovalService(provider, store)
        action = approved_refund(store)
        await service.execute(action.action_id)

        read = await service.refresh_status(action.action_id)

        assert (read.provider_status, read.attention) == ("succeeded", None)

    async def test_it_needs_no_writing(self, store):
        provider = FakeBillingProvider()
        action = approved(store)
        await ApprovalService(provider, store).execute(action.action_id)
        provider.can_write = False

        read = await ApprovalService(provider, store).refresh_status(action.action_id)

        assert read.provider_status == "succeeded"

    @pytest.mark.parametrize(
        ("state", "attention", "allocation"),
        [
            (ActionState.UNKNOWN_OUTCOME, "window_passed", "customer_balance"),
            (ActionState.UNKNOWN_OUTCOME, "window_unknown", None),
            (ActionState.EXECUTING, "window_unknown", None),
            (ActionState.FAILED, "window_unknown", None),
        ],
        ids=["unknown-past-the-window", "unknown-older", "executing-older", "failed-older"],
    )
    async def test_one_write_alike_settles_an_incident(self, store, state, attention, allocation):
        provider = FakeBillingProvider()
        action = await left_to_a_person(store, provider, state, attention, allocation=allocation)

        read = await ApprovalService(provider, store).refresh_status(action.action_id)

        assert read.state is ActionState.EXECUTED
        assert read.provider_action_id == provider.executed_actions[0].provider_action_id
        assert read.attention is None
        assert len(provider.executed_actions) == 1

    @pytest.mark.parametrize(
        "state", [ActionState.UNKNOWN_OUTCOME, ActionState.EXECUTING, ActionState.FAILED]
    )
    async def test_nothing_found_never_permits_a_write(self, store, state):
        provider = FakeBillingProvider()
        action = await left_to_a_person(store, provider, state, "window_unknown", written=False)
        service = ApprovalService(provider, store)

        read = await service.refresh_status(action.action_id)

        assert (read.state, read.attention) == (state, "window_unknown")
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        assert provider.executed_actions == []

    async def test_two_writes_found_stay_with_a_person(self, store):
        provider = FakeBillingProvider()
        action = await left_to_a_person(
            store, provider, ActionState.UNKNOWN_OUTCOME, "window_passed"
        )
        (made,) = provider.executed_actions
        provider.plant_action(made.model_copy(update={"provider_action_id": "cn_other"}))

        read = await ApprovalService(provider, store).refresh_status(action.action_id)

        assert (read.state, read.attention) == (ActionState.UNKNOWN_OUTCOME, "several_writes")

    async def test_a_write_unlike_the_plan_stays_with_a_person(self, store):
        provider = FakeBillingProvider()
        action = await left_to_a_person(
            store,
            provider,
            ActionState.UNKNOWN_OUTCOME,
            "window_passed",
            allocation="reduce_due",
        )

        read = await ApprovalService(provider, store).refresh_status(action.action_id)

        assert (read.state, read.attention) == (ActionState.UNKNOWN_OUTCOME, "unlike_the_action")

    async def test_an_action_without_a_write_or_an_incident_has_no_status(self, store):
        action = approved(store)

        with pytest.raises(IllegalTransition):
            await ApprovalService(FakeBillingProvider(), store).refresh_status(action.action_id)


T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


class Clock:
    """The time the service and the fake both read; a test moves it."""

    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


class Scripted(FakeBillingProvider):
    """A platform whose sends follow a script: each entry says what one
    send does -- "lost" (no write, no answer), "landed-lost" (written, no
    answer), "refused", "conflict" (an idempotency error), or "ok"."""

    def __init__(self, *script: str, clock: Clock | None = None) -> None:
        super().__init__()
        self.script = list(script)
        self.sends = 0
        self.hidden = 0
        if clock is not None:
            self.now = clock

    async def create_credit_note(self, *args, **kwargs):
        self.sends += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "lost":
            raise TimeoutError("connection dropped before the write")
        if step == "refused":
            raise ProviderRejected("permission")
        if step == "conflict":
            raise ProviderOutcomeUnknown("idempotency_error")
        made = await super().create_credit_note(*args, **kwargs)
        if step == "landed-lost":
            raise TimeoutError("no response from the billing platform")
        return made

    async def find_action(self, idempotency_key, invoice_id):
        if self.hidden:
            self.hidden -= 1
            return []
        return await super().find_action(idempotency_key, invoice_id)


def service_at(provider, store, clock):
    return ApprovalService(provider, store, now=clock)


class TestReconciliation:
    """specs/002-stripe-writes, FR-004 and FR-005: what reconciliation may
    conclude from what the platform holds, and when."""

    async def test_one_write_alike_is_executed_with_its_status(self, store):
        clock = Clock()
        provider = Scripted("landed-lost", clock=clock)
        action = approved(store)
        with pytest.raises(TimeoutError):
            await service_at(provider, store, clock).execute(action.action_id)

        held = await service_at(provider, store, clock).reconcile(action.action_id)

        assert held.state is ActionState.EXECUTED
        assert held.provider_status == "succeeded"
        assert held.provider_status_at == T0
        assert "reconciled" in held.resolution
        assert provider.sends == 1

    async def test_one_write_unlike_the_action_stays_with_a_person(self, store):
        clock = Clock()
        provider = Scripted("lost", clock=clock)
        action = approved(store)
        with pytest.raises(TimeoutError):
            await service_at(provider, store, clock).execute(action.action_id)
        await written_behind(provider, action)
        (made,) = provider.executed_actions
        provider._actions.clear()
        provider.plant_action(made.model_copy(update={"amount_minor": 100}))

        held = await service_at(provider, store, clock).reconcile(action.action_id)

        assert (held.state, held.attention) == (ActionState.UNKNOWN_OUTCOME, "unlike_the_action")

    async def test_an_incident_then_nothing_found_never_fails_nor_sends(self, store):
        clock = Clock()
        provider = Scripted("lost", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        store.annotate(action.action_id, ActionState.UNKNOWN_OUTCOME, attention="unlike_the_action")

        held = await service.reconcile(action.action_id)
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)

        assert (held.state, held.attention) == (ActionState.UNKNOWN_OUTCOME, "unlike_the_action")
        assert provider.sends == 1

    async def test_nothing_found_within_the_window_fails_and_is_retried_under_the_key(self, store):
        clock = Clock()
        provider = Scripted("lost", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        clock.advance(hours=22, minutes=59)

        done = await service.execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert provider.sends == 2
        assert done.first_attempt_at == T0

    async def test_nothing_found_past_the_window_proves_nothing(self, store):
        clock = Clock()
        provider = Scripted("lost", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        clock.advance(hours=23)

        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)

        held = store.get(action.action_id)
        assert (held.state, held.attention) == (ActionState.UNKNOWN_OUTCOME, "window_passed")
        assert provider.sends == 1

    async def test_a_write_found_with_no_allocation_planned_is_never_settled_alone(self, store):
        provider = FakeBillingProvider()
        action = approved(store)
        store.transition(
            action.action_id, ActionState.EXECUTING, first_attempt_at=datetime.now(UTC)
        )
        await written_behind(provider, action)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME)

        held = await ApprovalService(provider, store).reconcile(action.action_id)

        assert (held.state, held.attention) == (ActionState.UNKNOWN_OUTCOME, "window_unknown")

    async def test_an_abandoned_claim_within_the_window_fails(self, store):
        clock = Clock()
        provider = Scripted(clock=clock)
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)

        held = await ApprovalService(
            provider, store, stale_after=timedelta(0), now=clock
        ).reconcile(action.action_id)

        assert held.state is ActionState.FAILED
        assert held.uncertain_since == T0

    async def test_an_abandoned_claim_past_the_window_proves_nothing(self, store):
        clock = Clock()
        provider = Scripted(clock=clock)
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        clock.advance(hours=30)

        held = await ApprovalService(
            provider, store, stale_after=timedelta(0), now=clock
        ).reconcile(action.action_id)

        assert (held.state, held.attention) == (ActionState.UNKNOWN_OUTCOME, "window_passed")

    async def test_a_read_that_fails_leaves_the_state_as_it_was(self, store):
        class Unreachable(Scripted):
            async def find_action(self, idempotency_key, invoice_id):
                raise ProviderUnavailable("no answer")

        clock = Clock()
        provider = Unreachable("lost", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)

        with pytest.raises(ProviderUnavailable):
            await service.reconcile(action.action_id)

        assert store.get(action.action_id).state is ActionState.UNKNOWN_OUTCOME

    async def test_reconciliation_never_writes(self, store):
        class NoWrite(Scripted):
            async def create_credit_note(self, *args, **kwargs):
                raise AssertionError("reconciliation wrote")

        clock = Clock()
        provider = NoWrite(clock=clock)
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)

        held = await service_at(provider, store, clock).reconcile(action.action_id)

        assert held.state is ActionState.FAILED


class TestTheWindowBoundsEverySend:
    """specs/002-stripe-writes, FR-005: every send within 23 hours of the
    first attempt, a retry and a restart included."""

    async def test_no_retry_past_the_window(self, store):
        clock = Clock()
        provider = Scripted("refused", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(ProviderRejected):
            await service.execute(action.action_id)
        clock.advance(hours=23)

        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)

        held = store.get(action.action_id)
        assert (held.state, held.attention) == (ActionState.FAILED, "window_passed")
        assert provider.sends == 1

    async def test_no_retry_past_the_window_after_a_restart(self, tmp_path):
        clock = Clock()
        provider = Scripted("refused", clock=clock)
        database = tmp_path / "actions.db"
        before = SqliteActionStore(database)
        action = approved(before)
        with pytest.raises(ProviderRejected):
            await service_at(provider, before, clock).execute(action.action_id)
        before.close()
        clock.advance(hours=24)

        after = SqliteActionStore(database)
        with pytest.raises(NeedsAPerson):
            await service_at(provider, after, clock).execute(action.action_id)
        after.close()

        assert provider.sends == 1

    async def test_a_send_whose_deadline_passes_during_its_reads_goes_nowhere(self, store):
        clock = Clock()
        provider = Scripted(clock=Clock(T0 + timedelta(hours=24)))
        action = approved(store)
        service = service_at(provider, store, clock)

        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)

        held = store.get(action.action_id)
        assert held.attention == "window_passed"
        assert held.state is ActionState.FAILED
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        assert provider.executed_actions == []

    async def test_an_action_older_than_the_columns_is_never_sent(self, store):
        provider = Scripted()
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING)
        store.transition(action.action_id, ActionState.FAILED)
        store.annotate(action.action_id, ActionState.FAILED, attention="window_unknown")

        with pytest.raises(NeedsAPerson):
            await ApprovalService(provider, store).execute(action.action_id)

        assert provider.sends == 0


class TestARefusalAfterAnUnknownOutcome:
    """specs/002-stripe-writes, FR-006: a refusal settles nothing when an
    earlier attempt's outcome was unknown."""

    async def test_an_idempotency_conflict_after_a_lost_answer_is_unknown(self, store):
        clock = Clock()
        provider = Scripted("lost", "conflict", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)

        with pytest.raises(ProviderOutcomeUnknown):
            await service.execute(action.action_id)

        assert store.get(action.action_id).state is ActionState.UNKNOWN_OUTCOME

    async def test_landed_lost_then_refused_is_reconciled_as_executed(self, store):
        clock = Clock()
        provider = Scripted("landed-lost", "refused", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        provider.hidden = 1  # the first look misses it

        done = await service.execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert len(provider.executed_actions) == 1
        assert provider.sends == 2

    async def test_refused_after_a_lost_answer_with_nothing_found_fails(self, store):
        clock = Clock()
        provider = Scripted("lost", "refused", clock=clock)
        action = approved(store)
        service = service_at(provider, store, clock)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)

        with pytest.raises(ProviderRejected):
            await service.execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.FAILED
        assert "permission" in held.failure_reason

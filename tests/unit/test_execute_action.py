"""The one path by which money moves: what may and may not become EXECUTED."""

import asyncio
from datetime import timedelta

import pytest

from app.application.execute_action import ApprovalService
from app.domain.enums import ActionState
from app.domain.money import Money
from app.infrastructure.actions import IllegalTransition, SqliteActionStore
from app.providers.base import ProviderRejected
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
        store.transition(action.action_id, ActionState.EXECUTING)
        await provider.create_credit_note(
            invoice_id=action.invoice_id,
            amount_minor=action.amount.amount_minor,
            currency=action.amount.currency,
            reason=action.reason,
            idempotency_key=action.idempotency_key,
        )

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
        store.transition(action.action_id, ActionState.EXECUTING)
        await provider.create_credit_note(
            invoice_id=action.invoice_id,
            amount_minor=action.amount.amount_minor,
            currency=action.amount.currency,
            reason=action.reason,
            idempotency_key=action.idempotency_key,
        )

        fresh = ApprovalService(provider, store)
        with pytest.raises(IllegalTransition):
            await fresh.execute(action.action_id)

        late = ApprovalService(provider, store, stale_after=timedelta(0))
        done = await late.execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert len(provider.executed_actions) == 1


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

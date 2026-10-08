"""The one path by which money moves: what may and may not become EXECUTED."""

import asyncio

import pytest

from app.application.execute_action import ApprovalService
from app.domain.enums import ActionState
from app.domain.money import Money
from app.infrastructure.actions import IllegalTransition, SqliteActionStore
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
            raise ConnectionError("provider refused the write")
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

        with pytest.raises(ConnectionError):
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
            with pytest.raises(ConnectionError):
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

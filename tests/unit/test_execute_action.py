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

    async def find_action(self, idempotency_key):
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

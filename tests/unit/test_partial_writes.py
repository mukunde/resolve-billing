"""The execution core for a platform that writes in several requests with no
idempotency key (specs/015-pennylane-credit-notes, plan section 2): writes by
type, no window, partial writes, the per-customer blocker, the step hook, the
approved breakdown, and a person's resolution. On the fake, made to behave as
such a platform; Stripe and the fake as they are keep their own suites."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.execute_action import IN_FLIGHT, STEP_DEADLINE, ApprovalService, _unlike
from app.domain.enums import ActionState
from app.domain.models import TaxBreakdown
from app.domain.money import Money
from app.infrastructure.actions import (
    BlockedByAnother,
    IllegalTransition,
    NeedsAPerson,
    ResolutionRefused,
    SqliteActionStore,
    WritesNotSupported,
)
from app.providers.base import NotSent, PartialWrite, ProviderAction, ProviderRejected
from app.providers.fake.adapter import FakeBillingProvider

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
EUR = "EUR"


def money(minor: int) -> Money:
    return Money(amount_minor=minor, currency=EUR)


class Clock:
    """The service's clock, moved by the test."""

    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def store():
    actions = SqliteActionStore(":memory:")
    yield actions
    actions.close()


@pytest.fixture
def clock():
    return Clock()


def approved(
    store: SqliteActionStore,
    invoice_id: str = "inv_002",
    action_type: str = "credit_note",
    amount: int = 9900,
    customer_id: str | None = "cus_acme",
    tax_breakdown: TaxBreakdown | None = None,
):
    action = store.create(
        run_id="run_test",
        action_type=action_type,
        invoice_id=invoice_id,
        amount=money(amount),
        reason="duplicate invoice",
        provider="fake",
        customer_id=customer_id,
        tax_breakdown=tax_breakdown,
    )
    return store.transition(action.action_id, ActionState.APPROVED, approved_by="approver")


class Recorded(FakeBillingProvider):
    """The fake, recording what each write was given and every read."""

    def __init__(self) -> None:
        super().__init__()
        self.sends: list[dict] = []
        self.touched: list[str] = []

    async def find_invoice(self, invoice_id):
        self.touched.append("find_invoice")
        return await super().find_invoice(invoice_id)

    async def find_action(self, idempotency_key, invoice_id):
        self.touched.append("find_action")
        return await super().find_action(idempotency_key, invoice_id)

    async def create_credit_note(self, **kwargs):
        self.sends.append(kwargs)
        return await super().create_credit_note(**kwargs)

    async def create_refund(self, **kwargs):
        self.sends.append(kwargs)
        return await super().create_refund(**kwargs)


class CreditNotesOnly(Recorded):
    """A platform that writes credit notes and no refund (Pennylane)."""

    writes = frozenset({"credit_note"})


class NoWindow(Recorded):
    """A platform that keeps no key: its answers may be lost, and a write
    that landed may not show yet (`hidden`)."""

    idempotency_window = None

    def __init__(self) -> None:
        super().__init__()
        self.lose = False
        self.hidden = False

    async def _writable(self, invoice_id, allocation, send_by):
        # No window: no deadline to send by; the fake's own check needs one.
        assert send_by is None, "a platform with no window is given no deadline"
        return await super()._writable(invoice_id, allocation, datetime.max.replace(tzinfo=UTC))

    async def create_credit_note(self, **kwargs):
        made = await super().create_credit_note(**kwargs)
        if self.lose:
            raise TimeoutError("no answer from the billing platform")
        return made

    async def find_action(self, idempotency_key, invoice_id):
        found = await super().find_action(idempotency_key, invoice_id)
        return [] if self.hidden else found


class LeftHalfway(NoWindow):
    """Calls the hook before each request, makes a draft, then stops."""

    async def create_credit_note(self, **kwargs):
        self.sends.append(kwargs)
        await kwargs["before_request"]()
        await kwargs["before_request"]()
        raise PartialWrite("doc_7", "linking")


class Hooked(NoWindow):
    """Calls the hook before each of its requests; `between` runs after the
    first, as another process acting meanwhile would."""

    def __init__(self, between=None) -> None:
        super().__init__()
        self.between = between
        self.requests = 0

    async def create_credit_note(self, **kwargs):
        hook = kwargs["before_request"]
        await hook()
        self.requests += 1
        if self.between is not None:
            self.between()
        await hook()
        self.requests += 1
        return await FakeBillingProvider.create_credit_note(self, **kwargs)


def found_partial(action, stage="draft") -> ProviderAction:
    return ProviderAction(
        provider="fake",
        provider_action_id="doc_9",
        action_type="credit_note",
        idempotency_key=action.idempotency_key,
        status="succeeded",
        created_at=T0,
        invoice_id=action.invoice_id,
        amount_minor=action.amount.amount_minor,
        currency=EUR,
        test_mode=True,
        allocation="customer_balance",
        action_id=action.action_id,
        observed_at=T0,
        partial=stage,
    )


# -- FR-001: a type the platform does not write ------------------------------


class TestATypeNotWrittenIsRefusedFirst:
    async def test_execute_refuses_a_refund_before_any_read_or_claim(self, store):
        provider = CreditNotesOnly()
        action = approved(store, action_type="refund")

        with pytest.raises(WritesNotSupported) as refused:
            await ApprovalService(provider, store).execute(action.action_id)

        assert "outside ResolveBilling" in str(refused.value)
        assert provider.touched == [] and provider.sends == []
        held = store.get(action.action_id)
        assert held.state is ActionState.APPROVED
        assert held.first_attempt_at is None and held.claim_token is None

    async def test_reconcile_refuses_it_before_reading(self, store):
        provider = CreditNotesOnly()
        action = approved(store, action_type="refund")
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)

        with pytest.raises(WritesNotSupported):
            await ApprovalService(provider, store).reconcile(action.action_id)

        assert provider.touched == []
        assert store.get(action.action_id).state is ActionState.UNKNOWN_OUTCOME

    async def test_the_type_it_writes_is_written(self, store):
        provider = CreditNotesOnly()
        action = approved(store)

        done = await ApprovalService(provider, store).execute(action.action_id)

        assert done.state is ActionState.EXECUTED

    async def test_the_fake_still_writes_both(self, store):
        provider = Recorded()
        service = ApprovalService(provider, store)
        credit = approved(store, invoice_id="inv_001", amount=4000)
        refund = approved(store, invoice_id="inv_002", action_type="refund", amount=5000)

        assert (await service.execute(credit.action_id)).state is ActionState.EXECUTED
        assert (await service.execute(refund.action_id)).state is ActionState.EXECUTED
        assert service.writes("credit_note") and service.writes("refund")

    def test_a_platform_writing_nothing_writes_no_type(self, store):
        class ReadOnly(FakeBillingProvider):
            can_write = False

        service = ApprovalService(ReadOnly(), store)

        assert not service.writes("credit_note")
        assert not service.writes("refund")


# -- FR-007: no window ----------------------------------------------------------


class TestNoWindow:
    async def test_the_first_send_happens_with_no_deadline(self, store):
        provider = NoWindow()
        action = approved(store)

        done = await ApprovalService(provider, store).execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        (sent,) = provider.sends
        assert sent["send_by"] is None

    async def test_an_absence_after_a_lost_answer_is_never_failed_nor_resent(self, store):
        provider = NoWindow()
        provider.lose = True
        provider.hidden = True
        service = ApprovalService(provider, store)
        action = approved(store)

        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        with pytest.raises(NeedsAPerson) as stopped:
            await service.execute(action.action_id)

        assert stopped.value.attention == "no_trace"
        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "no_trace"
        assert IN_FLIGHT in held.resolution
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        assert len(provider.sends) == 1, "a creation is never resent"

    async def test_reconcile_alone_leaves_it_to_a_person(self, store):
        provider = NoWindow()
        provider.hidden = True
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)

        settled = await ApprovalService(provider, store).reconcile(action.action_id)

        assert settled.state is ActionState.UNKNOWN_OUTCOME
        assert settled.attention == "no_trace"

    async def test_a_document_that_lands_late_is_settled_by_a_status_read(self, store):
        provider = NoWindow()
        provider.lose = True
        provider.hidden = True
        service = ApprovalService(provider, store)
        action = approved(store)
        with pytest.raises(TimeoutError):
            await service.execute(action.action_id)
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)

        provider.hidden = False
        read = await service.refresh_status(action.action_id)

        assert read.state is ActionState.EXECUTED
        assert read.provider_action_id is not None
        assert len(provider.sends) == 1

    async def test_a_retry_after_a_plain_refusal_is_sent_again(self, store):
        """A refusal made nothing: under no window, a retry is a first send
        of its own, never the resend of a creation that may have landed."""

        class RefusedOnce(NoWindow):
            async def create_credit_note(self, **kwargs):
                if not self.sends:
                    self.sends.append(kwargs)
                    raise ProviderRejected("cap")
                return await super().create_credit_note(**kwargs)

        provider = RefusedOnce()
        service = ApprovalService(provider, store)
        action = approved(store)
        with pytest.raises(ProviderRejected):
            await service.execute(action.action_id)
        assert store.get(action.action_id).state is ActionState.FAILED

        done = await service.execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert done.attention is None


# -- FR-006: a document left half-way -----------------------------------------


class TestAPartialWrite:
    async def test_it_is_unknown_outcome_with_the_document_and_the_warning(self, store, clock):
        provider = LeftHalfway()
        service = ApprovalService(provider, store, now=clock)
        action = approved(store)

        with pytest.raises(NeedsAPerson) as stopped:
            await service.execute(action.action_id)

        assert stopped.value.attention == "partial_write"
        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "partial_write"
        assert held.partial_document == {"document_id": "doc_7", "stage": "linking"}
        assert "doc_7" in held.resolution and "linking" in held.resolution
        assert IN_FLIGHT in held.resolution
        assert held.last_request_at == T0

    async def test_it_is_final_for_everything_automatic(self, store):
        provider = LeftHalfway()
        service = ApprovalService(provider, store)
        action = approved(store)
        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        provider.touched.clear()

        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        with pytest.raises(NeedsAPerson):
            await service.reconcile(action.action_id)

        assert provider.touched == []
        assert len(provider.sends) == 1

    async def test_a_partial_document_found_by_reconciliation_is_never_success(self, store):
        provider = NoWindow()
        action = approved(store)
        provider.plant_action(found_partial(action))
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.annotate(action.action_id, ActionState.EXECUTING, allocation="customer_balance")
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)

        settled = await ApprovalService(provider, store).reconcile(action.action_id)

        assert settled.state is ActionState.UNKNOWN_OUTCOME
        assert settled.attention == "partial_write"
        assert settled.partial_document == {"document_id": "doc_9", "stage": "draft"}

    async def test_a_partial_document_found_by_a_status_read_is_never_success(self, store):
        provider = NoWindow()
        provider.hidden = True
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.annotate(action.action_id, ActionState.EXECUTING, allocation="customer_balance")
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)
        service = ApprovalService(provider, store)
        await service.reconcile(action.action_id)
        provider.hidden = False
        provider.plant_action(found_partial(action, stage="unlinked"))

        read = await service.refresh_status(action.action_id)

        assert read.state is ActionState.UNKNOWN_OUTCOME
        assert read.attention == "partial_write"
        assert read.partial_document == {"document_id": "doc_9", "stage": "unlinked"}
        assert read.status_read_found == "doc_9, not completed (unlinked)"

    async def test_a_partial_write_found_beside_an_executed_one_is_an_incident(self, store):
        provider = Recorded()
        service = ApprovalService(provider, store)
        action = approved(store)
        done = await service.execute(action.action_id)
        (made,) = provider.executed_actions
        provider._actions[action.idempotency_key] = made.model_copy(update={"partial": "draft"})

        read = await service.refresh_status(done.action_id)

        assert read.state is ActionState.EXECUTED
        assert read.attention == "partial_write"

    async def test_a_partial_answer_from_the_write_itself_is_never_success(self, store):
        class AnswersADraft(NoWindow):
            async def create_credit_note(self, **kwargs):
                made = await super().create_credit_note(**kwargs)
                return made.model_copy(update={"partial": "draft"})

        action = approved(store)

        with pytest.raises(NeedsAPerson):
            await ApprovalService(AnswersADraft(), store).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "partial_write"
        assert held.provider_action_id is None


# -- FR-010: the approved breakdown --------------------------------------------

TAXED = TaxBreakdown(
    excluding_tax=money(10000),
    taxes=((Decimal("20"), money(2000)),),
    total=money(12000),
)
ZERO_TAX = TaxBreakdown(
    excluding_tax=money(9900),
    taxes=((Decimal("0"), money(0)),),
    total=money(9900),
)


class TestTheBreakdown:
    async def test_the_approved_breakdown_reaches_the_write(self, store):
        provider = Recorded()
        action = approved(store, invoice_id="inv_600", amount=12000, customer_id="cus_vega",
                          tax_breakdown=TAXED)

        await ApprovalService(provider, store).execute(action.action_id)

        (sent,) = provider.sends
        assert sent["breakdown"] == TAXED
        assert callable(sent["before_request"])

    def _made(self, action, breakdown):
        return found_partial(action).model_copy(
            update={"partial": None, "breakdown": breakdown, "allocation": "customer_balance"}
        )

    def test_a_breakdown_reported_equal_is_alike(self, store):
        action = approved(store, invoice_id="inv_600", amount=12000, tax_breakdown=TAXED)

        assert _unlike(action, "customer_balance", self._made(action, TAXED)) is None

    def test_a_breakdown_reported_different_is_unlike(self, store):
        action = approved(store, invoice_id="inv_600", amount=12000, tax_breakdown=TAXED)
        other = TaxBreakdown(
            excluding_tax=money(10001),
            taxes=((Decimal("20"), money(1999)),),
            total=money(12000),
        )

        assert _unlike(action, "customer_balance", self._made(action, other)) == (
            "unlike_the_action"
        )

    def test_a_taxed_breakdown_against_an_untaxed_approval_is_unlike(self, store):
        action = approved(store, amount=12000)

        assert _unlike(action, "customer_balance", self._made(action, TAXED)) == (
            "unlike_the_action"
        )

    def test_an_all_zero_tax_is_untaxed(self, store):
        action = approved(store)

        assert _unlike(action, "customer_balance", self._made(action, ZERO_TAX)) is None

    def test_an_observed_untaxed_credit_against_a_taxed_approval_is_unlike(self, store):
        """Codex r389: a platform that reports its breakdown and observed no tax
        (None, normalised) is compared, never taken as "reports none"."""
        action = approved(store, invoice_id="inv_600", amount=12000, tax_breakdown=TAXED)
        made = self._made(action, None).model_copy(update={"breakdown_reported": True})

        assert _unlike(action, "customer_balance", made) == "unlike_the_action"

    def test_an_observed_untaxed_credit_against_an_untaxed_approval_is_alike(self, store):
        action = approved(store)
        made = self._made(action, None).model_copy(update={"breakdown_reported": True})

        assert _unlike(action, "customer_balance", made) is None

    def test_no_breakdown_reported_is_compared_as_before(self, store):
        action = approved(store, invoice_id="inv_600", amount=12000, tax_breakdown=TAXED)

        assert _unlike(action, "customer_balance", self._made(action, None)) is None

    def test_a_refund_is_compared_as_before(self, store):
        action = approved(store, action_type="refund")
        made = self._made(action, TAXED).model_copy(
            update={"action_type": "refund", "allocation": "refund"}
        )

        assert _unlike(action, "refund", made) is None

    async def test_an_unlike_breakdown_in_the_answer_is_an_incident(self, store):
        class ReportsAnother(Recorded):
            async def create_credit_note(self, **kwargs):
                made = await super().create_credit_note(**kwargs)
                return made.model_copy(update={"breakdown": TAXED})

        action = approved(store)

        with pytest.raises(NeedsAPerson):
            await ApprovalService(ReportsAnother(), store).execute(action.action_id)

        assert store.get(action.action_id).attention == "unlike_the_action"


# -- FR-009: the per-customer blocker ------------------------------------------


def unknown(store: SqliteActionStore, action) -> None:
    store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
    store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)


class TestTheBlocker:
    async def test_a_customer_with_an_unknown_credit_note_is_refused_before_sending(self, store):
        provider = Recorded()
        first = approved(store, invoice_id="inv_001")
        unknown(store, first)
        second = approved(store, invoice_id="inv_002")

        with pytest.raises(BlockedByAnother) as refused:
            await ApprovalService(provider, store).execute(second.action_id)

        assert refused.value.blocker == first.action_id
        assert first.action_id in str(refused.value)
        assert provider.sends == []
        held = store.get(second.action_id)
        assert held.state is ActionState.APPROVED
        assert held.first_attempt_at is None

    @pytest.mark.parametrize("attention", ["partial_write", "no_trace"])
    def test_a_partial_or_traceless_one_blocks_whatever_its_state(self, store, attention):
        first = approved(store, invoice_id="inv_001")
        unknown(store, first)
        store.mark_partial(first.action_id, attention=attention, resolution="x", at=T0)
        second = approved(store, invoice_id="inv_002")

        with pytest.raises(BlockedByAnother):
            store.transition(second.action_id, ActionState.EXECUTING)

    def test_an_executing_one_blocks(self, store):
        first = approved(store, invoice_id="inv_001")
        store.transition(first.action_id, ActionState.EXECUTING)
        second = approved(store, invoice_id="inv_002")

        with pytest.raises(BlockedByAnother):
            store.transition(second.action_id, ActionState.EXECUTING)

    def test_another_customer_is_not_blocked(self, store):
        first = approved(store, invoice_id="inv_001")
        unknown(store, first)
        other = approved(store, invoice_id="inv_010", customer_id="cus_nord", amount=4900)

        assert store.transition(other.action_id, ActionState.EXECUTING).claim_token

    def test_a_refund_neither_blocks_nor_is_blocked(self, store):
        refund = approved(store, invoice_id="inv_001", action_type="refund")
        unknown(store, refund)
        credit = approved(store, invoice_id="inv_002")
        store.transition(credit.action_id, ActionState.EXECUTING)
        another = approved(store, invoice_id="inv_002", action_type="refund", amount=100)

        assert store.transition(another.action_id, ActionState.EXECUTING).claim_token

    def test_older_rows_never_block_nor_are_blocked(self, store):
        older = approved(store, invoice_id="inv_001", customer_id=None)
        unknown(store, older)
        newer = approved(store, invoice_id="inv_002")
        store.transition(newer.action_id, ActionState.EXECUTING)
        another_older = approved(store, invoice_id="inv_002", amount=100, customer_id=None)

        assert store.transition(another_older.action_id, ActionState.EXECUTING).claim_token

    async def test_it_is_released_once_the_blocker_is_executed(self, store):
        provider = Recorded()
        service = ApprovalService(provider, store)
        first = approved(store, invoice_id="inv_001", amount=4000)
        store.transition(first.action_id, ActionState.EXECUTING)
        second = approved(store, invoice_id="inv_002", amount=4000)
        with pytest.raises(BlockedByAnother):
            await service.execute(second.action_id)
        store.transition(first.action_id, ActionState.FAILED, failure_reason="refused")

        await service.execute(first.action_id)
        done = await service.execute(second.action_id)

        assert done.state is ActionState.EXECUTED

    def test_two_connections_claiming_at_once_one_wins(self, tmp_path):
        database = tmp_path / "actions.db"
        setup = SqliteActionStore(database)
        first = approved(setup, invoice_id="inv_001")
        second = approved(setup, invoice_id="inv_002")
        setup.close()
        stores = [SqliteActionStore(database), SqliteActionStore(database)]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def claim(store, action_id):
            barrier.wait()
            try:
                store.transition(action_id, ActionState.EXECUTING)
                outcomes.append("claimed")
            except BlockedByAnother:
                outcomes.append("blocked")

        threads = [
            threading.Thread(target=claim, args=(stores[0], first.action_id)),
            threading.Thread(target=claim, args=(stores[1], second.action_id)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(outcomes) == ["blocked", "claimed"]
        check = SqliteActionStore(database)
        states = {check.get(a.action_id).state for a in (first, second)}
        assert states == {ActionState.EXECUTING, ActionState.APPROVED}
        for opened in (*stores, check):
            opened.close()

    def test_the_condition_is_in_the_claim_itself(self, tmp_path):
        """A blocker recorded by another connection after this one read the
        action still stops the claim: the check is in the UPDATE."""
        database = tmp_path / "actions.db"
        mine, theirs = SqliteActionStore(database), SqliteActionStore(database)
        first = approved(theirs, invoice_id="inv_001")
        second = approved(mine, invoice_id="inv_002")
        read = mine.get(second.action_id)
        theirs.transition(first.action_id, ActionState.EXECUTING)

        with pytest.raises(BlockedByAnother):
            mine.transition(second.action_id, ActionState.EXECUTING)

        assert read.state is ActionState.APPROVED
        assert mine.get(second.action_id).state is ActionState.APPROVED
        mine.close()
        theirs.close()

    @pytest.mark.parametrize("attention", ["partial_write", "no_trace"])
    def test_it_is_released_by_a_resolution_seen_from_another_connection(
        self, tmp_path, attention
    ):
        database = tmp_path / "actions.db"
        person, worker = SqliteActionStore(database), SqliteActionStore(database)
        first = approved(person, invoice_id="inv_001")
        unknown(person, first)
        person.mark_partial(first.action_id, attention=attention, resolution="x", at=T0)
        second = approved(worker, invoice_id="inv_002")
        with pytest.raises(BlockedByAnother):
            worker.transition(second.action_id, ActionState.EXECUTING)
        person.record_status_read(first.action_id, T0 + timedelta(minutes=11), "nothing")
        person.record_resolution(first.action_id, "m.dupont", "deleted the draft", T0)

        claimed = worker.transition(second.action_id, ActionState.EXECUTING)

        assert claimed.state is ActionState.EXECUTING
        assert person.get(first.action_id).attention == attention
        person.close()
        worker.close()

    def test_an_older_file_gains_the_columns_and_its_rows_never_block(self, tmp_path):
        database = tmp_path / "old.db"
        with sqlite3.connect(database) as old:
            old.executescript(
                """
                CREATE TABLE pending_actions (
                    action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    state TEXT NOT NULL, action_type TEXT NOT NULL,
                    invoice_id TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL, reason TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, approved_by TEXT,
                    provider_action_id TEXT, failure_reason TEXT
                );
                INSERT INTO pending_actions VALUES ('act_old', 'run', '2026-10-01T00:00:00+00:00',
                    '2026-10-01T00:00:00+00:00', 'executing', 'credit_note', 'inv_001', 9900,
                    'EUR', 'r', 'k_old', 'a', NULL, NULL);
                """
            )
        store = SqliteActionStore(database)
        old = store.get("act_old")
        newer = approved(store, invoice_id="inv_002")

        assert old.customer_id is None and old.resolved_at is None
        assert store.transition(newer.action_id, ActionState.EXECUTING).claim_token
        store.close()


# -- FR-011: the step hook -----------------------------------------------------


class TestTheStepHook:
    async def test_each_request_is_recorded_under_the_claim(self, store, clock):
        provider = Hooked(between=lambda: clock.advance(seconds=30))
        action = approved(store)

        done = await ApprovalService(provider, store, now=clock).execute(action.action_id)

        assert done.state is ActionState.EXECUTED
        assert provider.requests == 2
        assert done.last_request_at == T0 + timedelta(seconds=30)

    async def test_a_stale_claims_worker_sends_nothing_more(self, store, clock):
        """Another process took the claim over (an abandoned claim
        reconciled) between two requests of this attempt."""
        action = approved(store)

        def taken_over():
            store.transition(
                action.action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason="the claim was abandoned",
                uncertain_since=T0,
            )

        provider = Hooked(between=taken_over)

        with pytest.raises(IllegalTransition) as stopped:
            await ApprovalService(provider, store, now=clock).execute(action.action_id)

        assert "no longer held" in str(stopped.value)
        assert provider.requests == 1
        assert provider.executed_actions == []
        held = store.get(action.action_id)
        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.failure_reason == "the claim was abandoned"

    async def test_a_new_claim_stops_the_old_one(self, store, clock):
        action = approved(store)
        old = store.transition(action.action_id, ActionState.EXECUTING)
        store.transition(action.action_id, ActionState.FAILED, failure_reason="x")
        store.transition(action.action_id, ActionState.EXECUTING)

        assert not store.record_request(action.action_id, old.claim_token, T0)

    async def test_past_the_step_deadline_nothing_more_is_sent(self, store, clock):
        provider = Hooked(between=lambda: clock.advance(seconds=STEP_DEADLINE.total_seconds()))
        action = approved(store)

        with pytest.raises(NotSent):
            await ApprovalService(provider, store, now=clock).execute(action.action_id)

        assert provider.requests == 1
        assert provider.executed_actions == []
        held = store.get(action.action_id)
        assert held.state is ActionState.FAILED
        assert held.attention is None
        assert "step deadline" in held.failure_reason

    async def test_a_request_whose_timeout_would_pass_the_deadline_is_not_started(
        self, store, clock
    ):
        """Codex r389: the hook counts the platform's request timeout (FR-011)."""
        provider = Hooked(between=lambda: clock.advance(
            seconds=STEP_DEADLINE.total_seconds() - 5))
        provider.request_timeout = timedelta(seconds=10)
        action = approved(store)

        with pytest.raises(NotSent):
            await ApprovalService(provider, store, now=clock).execute(action.action_id)

        assert provider.requests == 1
        assert provider.executed_actions == []

    async def test_a_document_made_before_the_hook_refuses_is_a_partial_write(self, store, clock):
        class DraftThenHook(NoWindow):
            async def create_credit_note(self, **kwargs):
                await kwargs["before_request"]()
                clock.advance(minutes=3)
                try:
                    await kwargs["before_request"]()
                except NotSent as stopped:
                    raise PartialWrite("doc_3", "draft") from stopped

        action = approved(store)

        with pytest.raises(NeedsAPerson):
            await ApprovalService(DraftThenHook(), store, now=clock).execute(action.action_id)

        held = store.get(action.action_id)
        assert held.attention == "partial_write"
        assert held.partial_document == {"document_id": "doc_3", "stage": "draft"}

    async def test_reconciliation_and_status_reads_take_no_hook(self, store, clock):
        provider = NoWindow()
        provider.hidden = True
        action = approved(store)
        unknown(store, action)
        service = ApprovalService(provider, store, now=clock)
        clock.advance(hours=5)

        settled = await service.reconcile(action.action_id)
        read = await service.refresh_status(action.action_id)

        assert settled.attention == "no_trace"
        assert read.status_read_at == clock.at


# -- FR-009a: a person's resolution --------------------------------------------


async def left_halfway(store, clock):
    service = ApprovalService(LeftHalfway(), store, now=clock)
    action = approved(store)
    with pytest.raises(NeedsAPerson):
        await service.execute(action.action_id)
    return service, action


class TestAResolution:
    async def test_it_needs_a_status_read_ten_minutes_after_the_last_request(self, store, clock):
        service, action = await left_halfway(store, clock)
        with pytest.raises(ResolutionRefused):
            service.resolve(action.action_id, "m.dupont", "deleted the draft")
        clock.advance(minutes=9)
        await service.refresh_status(action.action_id)
        with pytest.raises(ResolutionRefused) as early:
            service.resolve(action.action_id, "m.dupont", "deleted the draft")
        assert "ten minutes" in str(early.value)

        clock.advance(minutes=2)
        await service.refresh_status(action.action_id)
        resolved = service.resolve(action.action_id, "m.dupont", "  deleted the draft ")

        assert resolved.resolved_by == "m.dupont"
        assert resolved.resolved_at == clock.at
        assert resolved.resolution_note == "deleted the draft"
        assert resolved.attention == "partial_write"
        assert resolved.state is ActionState.UNKNOWN_OUTCOME

    async def test_a_read_begun_too_soon_does_not_count_however_late_it_returns(
        self, store, clock
    ):
        service, action = await left_halfway(store, clock)

        class SlowRead(LeftHalfway):
            async def find_action(self, idempotency_key, invoice_id):
                clock.advance(minutes=5)
                return await super().find_action(idempotency_key, invoice_id)

        slow = ApprovalService(SlowRead(), store, now=clock)
        clock.advance(minutes=9)
        read = await slow.refresh_status(action.action_id)

        assert read.status_read_at == T0 + timedelta(minutes=9)
        with pytest.raises(ResolutionRefused):
            service.resolve(action.action_id, "m.dupont", "deleted the draft")

    async def test_it_is_recorded_once(self, store, clock):
        service, action = await left_halfway(store, clock)
        clock.advance(minutes=11)
        await service.refresh_status(action.action_id)
        service.resolve(action.action_id, "m.dupont", "deleted the draft")

        with pytest.raises(ResolutionRefused) as again:
            service.resolve(action.action_id, "j.martin", "something else")

        assert "already" in str(again.value)
        assert store.get(action.action_id).resolved_by == "m.dupont"

    async def test_it_is_refused_on_any_other_action(self, store, clock):
        service = ApprovalService(Recorded(), store, now=clock)
        action = approved(store)
        unknown(store, action)
        clock.advance(minutes=30)
        store.record_status_read(action.action_id, clock.at, "nothing")

        with pytest.raises(ResolutionRefused):
            service.resolve(action.action_id, "m.dupont", "nothing to do")

    async def test_an_empty_account_is_refused(self, store, clock):
        service, action = await left_halfway(store, clock)

        with pytest.raises(ValueError):
            service.resolve(action.action_id, "m.dupont", "   ")

    async def test_it_never_executes(self, store, clock):
        service, action = await left_halfway(store, clock)
        clock.advance(minutes=11)
        await service.refresh_status(action.action_id)
        service.resolve(action.action_id, "m.dupont", "deleted the draft")

        with pytest.raises(NeedsAPerson):
            await service.execute(action.action_id)
        with pytest.raises(NeedsAPerson):
            store.transition(action.action_id, ActionState.FAILED, unless_incident=True)

    async def test_it_releases_the_blocker(self, store, clock):
        service, action = await left_halfway(store, clock)
        provider = Recorded()
        other = approved(store, invoice_id="inv_001", amount=4000)
        with pytest.raises(BlockedByAnother):
            await ApprovalService(provider, store, now=clock).execute(other.action_id)
        clock.advance(minutes=11)
        await service.refresh_status(action.action_id)
        service.resolve(action.action_id, "m.dupont", "deleted the draft")

        done = await ApprovalService(provider, store, now=clock).execute(other.action_id)

        assert done.state is ActionState.EXECUTED

    async def test_without_a_recorded_request_the_unknown_outcome_counts(self, store, clock):
        """An adapter that never called the hook: the instant the outcome
        became unknown bounds every request of the attempt."""
        provider = NoWindow()
        provider.hidden = True
        service = ApprovalService(provider, store, now=clock)
        action = approved(store)
        unknown(store, action)
        await service.reconcile(action.action_id)
        clock.at = T0 + timedelta(minutes=9)
        await service.refresh_status(action.action_id)
        with pytest.raises(ResolutionRefused):
            service.resolve(action.action_id, "m.dupont", "checked")
        clock.at = T0 + timedelta(minutes=10)
        await service.refresh_status(action.action_id)

        assert service.resolve(action.action_id, "m.dupont", "checked").resolved_at

    async def test_a_status_read_works_with_writes_off(self, store, clock):
        _, action = await left_halfway(store, clock)

        class Off(NoWindow):
            can_write = False

        read = await ApprovalService(Off(), store, now=clock).refresh_status(action.action_id)

        assert read.status_read_at == clock.at
        assert read.status_read_found == "nothing under this idempotency key"

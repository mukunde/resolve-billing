"""The action store: what it keeps about a write's attempts, and how it
treats actions recorded before it kept them (specs/002-stripe-writes)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import ActionState
from app.domain.money import Money
from app.infrastructure.actions import IllegalTransition, SqliteActionStore

NINETY_NINE = Money(amount_minor=9900, currency="EUR")
T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


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


class TestTheFirstAttemptIsKept:
    def test_the_first_claim_records_its_instant_and_a_later_one_does_not(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.FAILED, failure_reason="no")

        later = T0 + timedelta(hours=2)
        held = store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=later)

        assert held.first_attempt_at == T0

    def test_the_allocation_is_stored_once(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.annotate(action.action_id, ActionState.EXECUTING, allocation="customer_balance")

        held = store.annotate(action.action_id, ActionState.EXECUTING, allocation="refund")

        assert held.allocation == "customer_balance"

    def test_the_first_uncertainty_is_kept(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)
        store.transition(action.action_id, ActionState.FAILED)
        store.transition(action.action_id, ActionState.EXECUTING)

        held = store.transition(
            action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0 + timedelta(hours=1)
        )

        assert held.uncertain_since == T0


class TestAnnotating:
    def test_an_annotation_changes_no_state(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=T0)

        held = store.annotate(
            action.action_id,
            ActionState.UNKNOWN_OUTCOME,
            attention="several_writes",
            resolution="two writes under the key",
        )

        assert held.state is ActionState.UNKNOWN_OUTCOME
        assert held.attention == "several_writes"
        assert held.resolution == "two writes under the key"

    def test_an_annotation_refuses_an_action_that_moved(self, store):
        action = approved(store)

        with pytest.raises(IllegalTransition):
            store.annotate(action.action_id, ActionState.EXECUTING, attention="window_passed")

        assert store.get(action.action_id).attention is None

    def test_an_annotation_can_clear_what_it_set(self, store):
        action = approved(store)
        store.annotate(action.action_id, ActionState.APPROVED, attention="not_successful")

        held = store.annotate(action.action_id, ActionState.APPROVED, attention=None)

        assert held.attention is None

    def test_an_attention_the_store_does_not_know_is_refused(self, store):
        action = approved(store)

        with pytest.raises(ValueError):
            store.annotate(action.action_id, ActionState.APPROVED, attention="whatever")


class TestAFailedActionIsRecordedExecutedOnlyWithProof:
    """A person's status read may find a write for an action marked FAILED
    (specs/002-stripe-writes, plan section 3). The store still refuses
    EXECUTED without the platform's reference."""

    def test_without_a_reference_it_is_refused(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.FAILED)

        with pytest.raises(IllegalTransition):
            store.transition(action.action_id, ActionState.EXECUTED)

        assert store.get(action.action_id).state is ActionState.FAILED

    def test_with_the_platforms_reference_it_is_recorded(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.FAILED)

        held = store.transition(
            action.action_id,
            ActionState.EXECUTED,
            provider_action_id="cn_001",
            provider_status="succeeded",
            provider_status_at=T0,
        )

        assert held.state is ActionState.EXECUTED
        assert (held.provider_status, held.provider_status_at) == ("succeeded", T0)


OLD_SCHEMA = """
CREATE TABLE pending_actions (
    action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, state TEXT NOT NULL, action_type TEXT NOT NULL,
    invoice_id TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
    reason TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, resume_url TEXT,
    approved_by TEXT, provider_action_id TEXT, failure_reason TEXT, resolution TEXT,
    expires_at TEXT, approved_fingerprint TEXT
);
"""


class TestActionsOlderThanTheColumns:
    """When an older action's first attempt was sent cannot be shown, so it
    is never sent again automatically (plan section 3)."""

    def test_attempted_actions_are_marked_and_the_others_left(self, tmp_path):
        database = tmp_path / "old.db"
        connection = sqlite3.connect(database)
        connection.executescript(OLD_SCHEMA)
        now = T0.isoformat()
        for number, state in enumerate(
            ("pending_approval", "approved", "executing", "unknown_outcome", "failed", "executed")
        ):
            connection.execute(
                "INSERT INTO pending_actions (action_id, run_id, created_at, updated_at, state, "
                "action_type, invoice_id, amount_minor, currency, reason, idempotency_key, "
                "provider_action_id) VALUES (?, 'run', ?, ?, ?, 'credit_note', 'inv_002', 9900, "
                "'EUR', 'x', ?, ?)",
                (
                    f"act_{state}",
                    now,
                    now,
                    state,
                    f"key-{number}",
                    "cn_001" if state == "executed" else None,
                ),
            )
        connection.commit()
        connection.close()

        store = SqliteActionStore(database)
        marks = {
            state: store.get(f"act_{state}").attention
            for state in (
                "pending_approval",
                "approved",
                "executing",
                "unknown_outcome",
                "failed",
                "executed",
            )
        }
        store.close()

        assert marks == {
            "pending_approval": None,
            "approved": None,
            "executing": "window_unknown",
            "unknown_outcome": "window_unknown",
            "failed": "window_unknown",
            "executed": None,
        }

    def test_opening_a_migrated_file_again_marks_nothing_new(self, tmp_path):
        database = tmp_path / "actions.db"
        store = SqliteActionStore(database)
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.FAILED)
        store.close()

        again = SqliteActionStore(database)
        assert again.get(action.action_id).attention is None
        again.close()


class TestATransitionCanClearAnIncident:
    def test_executed_with_no_attention_clears_it(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, attention="window_passed")

        held = store.transition(
            action.action_id, ActionState.EXECUTED, provider_action_id="cn_001", attention=None
        )

        assert held.attention is None

    def test_a_transition_that_says_nothing_of_it_keeps_it(self, store):
        action = approved(store)
        store.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        store.transition(action.action_id, ActionState.UNKNOWN_OUTCOME, attention="window_passed")

        held = store.transition(action.action_id, ActionState.FAILED)

        assert held.attention == "window_passed"


class TestAClaimSeesAnIncidentMadeElsewhere:
    """r137: the claim refuses an incident in the store itself, so one
    annotated by another process after this one read the action stops it."""

    def test_an_incident_annotated_by_another_connection_stops_the_claim(self, tmp_path):
        from app.infrastructure.actions import NeedsAPerson

        database = tmp_path / "actions.db"
        here, there = SqliteActionStore(database), SqliteActionStore(database)
        action = approved(here)
        here.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        here.transition(action.action_id, ActionState.FAILED)
        stale = here.get(action.action_id)
        there.annotate(action.action_id, ActionState.FAILED, attention="several_writes")
        here.get = lambda action_id: stale  # this process read before the annotation

        with pytest.raises(NeedsAPerson):
            here.transition(action.action_id, ActionState.EXECUTING)

        assert there.get(action.action_id).state is ActionState.FAILED
        here.close()
        there.close()

    def test_a_transition_keeps_an_attention_set_since_it_read(self, tmp_path):
        database = tmp_path / "actions.db"
        here, there = SqliteActionStore(database), SqliteActionStore(database)
        action = approved(here)
        here.transition(action.action_id, ActionState.EXECUTING, first_attempt_at=T0)
        stale = here.get(action.action_id)
        there.annotate(action.action_id, ActionState.EXECUTING, attention="window_passed")
        here.get = lambda action_id: stale if action_id == action.action_id else None

        here.transition(action.action_id, ActionState.FAILED)

        assert there.get(action.action_id).attention == "window_passed"
        here.close()
        there.close()

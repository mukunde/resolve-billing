"""Approve, reject, execute. The only path by which money moves.

Nothing else in the codebase calls ``create_credit_note`` or
``create_refund``. That is the point of ADR-004: the decision engine
recommends, the action policy authorises, and this single module executes --
after a human has said yes, and exactly the instrument the human approved.

Retry policy: reads retry freely, a write never retries blindly. The
platform either answers -- the write succeeded, or it was refused -- or it
does not, and then the outcome is unknown: the credit note may exist with its
response lost. An unknown outcome is settled by asking the platform what it
holds under the idempotency key, never by writing again on a guess. See
ADR-008.

An approval holds for what was shown, and not forever. A request carries a
deadline; past it, it can no longer be decided. The approver decides on a
fingerprint of the proposal and of the invoice as the platform presented
it; if either changed in between, or changes between approval and
execution, the action expires instead of going through on facts nobody
looked at.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.enums import ActionState
from app.domain.models import TaxBreakdown
from app.infrastructure.actions import (
    INCIDENTS,
    IllegalTransition,
    NeedsAPerson,
    PendingAction,
    SqliteActionStore,
    WritesNotSupported,
)
from app.providers.base import (
    ALLOCATIONS,
    BillingProvider,
    NotSent,
    PartialWrite,
    ProviderAction,
    ProviderRejected,
)

#: How long a claim on the write may go without a recorded result before it
#: is treated as abandoned -- a process that died between ringing the
#: provider and writing down the answer. Generous on purpose: reconciling a
#: claim that is still in flight is safe, since the idempotency key makes the
#: two writes one, but it is noise.
STALE_CLAIM = timedelta(minutes=5)

#: How long after its claim an attempt may still send a request; each
#: request's own timeout is counted by the adapter (spec 015, FR-011).
STEP_DEADLINE = timedelta(minutes=2)

#: Said wherever a document may have been left half-way, or a creation lost:
#: a request already sent cannot be cancelled on the platform's side.
IN_FLIGHT = (
    "a request may still be in flight on the platform's side: read the status again "
    "at least ten minutes after the attempt's last request before recording how it "
    "was resolved; nothing automatic follows"
)

#: The longest account of a resolution a person may record.
RESOLUTION_NOTE_LIMIT = 2000


def breakdown_holds(action: PendingAction, invoice) -> bool:
    """Whether the invoice read now carries the tax breakdown recorded with
    the proposal -- None on both sides for an untaxed one."""
    current = TaxBreakdown.of(invoice) if invoice is not None else None
    return current == action.tax_breakdown


class ApprovalService:
    def __init__(
        self,
        provider: BillingProvider,
        actions: SqliteActionStore,
        stale_after: timedelta = STALE_CLAIM,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._actions = actions
        self._stale_after = stale_after
        self._now = now or (lambda: datetime.now(UTC))

    def writes(self, action_type: str) -> bool:
        """Whether the platform writes actions of this type. One it does not
        is a person's to carry out outside ResolveBilling (spec 015, FR-001)."""
        return bool(self._provider.can_write) and action_type in self._provider.writes

    def _refuse_unwritten(self, action: PendingAction) -> None:
        if not self.writes(action.action_type):
            raise WritesNotSupported(
                action.action_id,
                action.state,
                action.action_type if self._provider.can_write else None,
            )

    async def proposal_fingerprint(self, action: PendingAction) -> str:
        """What an approver is asked to approve, reduced to a comparable value.

        The proposal itself -- instrument, invoice, amount -- and the invoice
        as the platform holds it now: status, total, lines. Read from the
        platform, not from a copy of ours, so a change made there shows.
        """
        invoice = await self._provider.find_invoice(action.invoice_id)
        return self._fingerprint(action, invoice)

    async def presented(self, action: PendingAction) -> str | None:
        """What an approval must name, read once with the invoice: the
        proposal's fingerprint -- or None when the invoice no longer carries
        the tax recorded with the proposal, which is then not offered for
        approval at all (specs/013-invoice-tax, FR-007).
        """
        invoice = await self._provider.find_invoice(action.invoice_id)
        if not breakdown_holds(action, invoice):
            return None
        return self._fingerprint(action, invoice)

    @staticmethod
    def _fingerprint(action: PendingAction, invoice) -> str:
        material = {
            "action_type": action.action_type,
            "invoice_id": action.invoice_id,
            "amount_minor": action.amount.amount_minor,
            "currency": action.amount.currency,
            "invoice": None if invoice is None else invoice.model_dump(mode="json"),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()[:32]

    async def approve(self, action_id: str, approved_by: str, presented: str) -> PendingAction:
        """Record who said yes, to what. Approving does not execute.

        Two steps rather than one so that the audit answers "who authorised
        this" separately from "did it go through", and so a provider outage
        does not force a second human decision.

        ``presented`` is the fingerprint of the proposal as it was shown to
        the approver. If the proposal no longer matches it, the yes was given
        to something else: the action expires, and the dispute goes back to
        be investigated on today's facts.
        """
        # Past its deadline, the request is expired and the transition
        # below refuses it.
        action = self.expire_if_overdue(action_id)
        invoice = await self._provider.find_invoice(action.invoice_id)
        current = self._fingerprint(action, invoice)
        if action.state is ActionState.PENDING_APPROVAL and not breakdown_holds(action, invoice):
            # The tax shown is the one recorded when the proposal was made;
            # an invoice whose tax is no longer that one is not what was
            # proposed (specs/013-invoice-tax, FR-007).
            self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    "the invoice's tax is no longer the one proposed; "
                    "approval refused, to be investigated again"
                ),
            )
            raise IllegalTransition(
                action_id,
                ActionState.EXPIRED,
                ActionState.APPROVED,
                why="(the invoice's tax changed after the proposal)",
            )
        if action.state is ActionState.PENDING_APPROVAL and presented != current:
            self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    "the proposal or its invoice changed after it was presented; "
                    "approval refused, to be investigated again"
                ),
            )
            raise IllegalTransition(
                action_id,
                ActionState.EXPIRED,
                ActionState.APPROVED,
                why="(the proposal changed after it was presented)",
            )
        return self._actions.transition(
            action_id,
            ActionState.APPROVED,
            approved_by=approved_by,
            approved_fingerprint=current,
        )

    def reject(self, action_id: str, rejected_by: str) -> PendingAction:
        self.expire_if_overdue(action_id)
        return self._actions.transition(action_id, ActionState.REJECTED, approved_by=rejected_by)

    def expire_if_overdue(self, action_id: str) -> PendingAction:
        """Expire a request nobody decided in time; return the action.

        Checked when someone tries to decide, not by a sweeper: a request
        that is never opened again does no harm by staying pending, and one
        that is opened finds out then.
        """
        action = self._actions.get(action_id)
        if action.state is ActionState.PENDING_APPROVAL and action.expired():
            return self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    f"no decision before the deadline, {action.expires_at:%Y-%m-%d %H:%M} UTC"
                ),
            )
        return action

    async def execute(self, action_id: str) -> PendingAction:
        """Carry out an approved action against the billing platform."""
        action = self._actions.get(action_id)

        # Replaying an execute on something already done returns it unchanged
        # rather than transitioning again -- the caller may simply be a retry
        # that lost the first response.
        if action.state is ActionState.EXECUTED:
            return action

        # A platform that does not write this type of action -- or nothing at
        # all -- is never asked: refused here, before any read, reconciliation
        # or claim, so nothing has happened (spec 015, FR-001).
        self._refuse_unwritten(action)

        # An incident stops everything automatic for the action, for good:
        # nothing is reconciled, read or sent on a retry. A person decides.
        if action.attention in INCIDENTS:
            raise NeedsAPerson(action_id, action.state, action.attention)

        # The last attempt got no answer, or a claim was abandoned by a
        # process that died mid-write. Find out what happened before anything
        # else: if the write landed, it is recorded and returned; if not, the
        # action is FAILED and the write is made again below. A claim still
        # in flight is left alone, and refused below.
        if action.state in (ActionState.UNKNOWN_OUTCOME, ActionState.EXECUTING):
            action = await self.reconcile(action_id)
            if action.state is ActionState.EXECUTED:
                return action
            if action.attention in INCIDENTS:
                raise NeedsAPerson(action_id, action.state, action.attention)

        # An approved action is executed; a failed one is retried, through the
        # provider and with the same idempotency key, never by marking it
        # done. Anything else -- awaiting approval, rejected, expired, being
        # executed -- is refused with its current state, which is what a
        # caller needs in order to know whether to retry.
        if action.state not in (ActionState.APPROVED, ActionState.FAILED):
            raise IllegalTransition(action_id, action.state, ActionState.EXECUTED)

        # The approval was given to a proposal on a given invoice. If the
        # invoice changed since -- paid, voided, re-issued -- it no longer
        # covers this write: the action expires and nothing is claimed. An
        # approval recorded before fingerprints existed carries none, and is
        # held to the guards below only.
        if (
            action.approved_fingerprint is not None
            and await self.proposal_fingerprint(action) != action.approved_fingerprint
        ):
            self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    "the invoice changed after approval; nothing written, to be investigated again"
                ),
            )
            raise IllegalTransition(
                action_id,
                ActionState.EXPIRED,
                ActionState.EXECUTED,
                why="(the invoice changed after approval)",
            )

        # The write is the one the human approved, by type. A type with no
        # write on the platform is refused before anything is claimed: never
        # replaced by the nearest instrument that does exist.
        writes = {
            "credit_note": self._provider.create_credit_note,
            "refund": self._provider.create_refund,
        }
        write = writes.get(action.action_type)
        if write is None:
            raise IllegalTransition(
                action_id,
                action.state,
                ActionState.EXECUTED,
                why=f"(no write on the platform for action type {action.action_type!r})",
            )

        # One guard of our own, before anything is claimed: never more than
        # the invoice itself. The platform holds the ledger and caps what an
        # invoice can still give back (ADR-008); this only stops a proposal
        # bug from relying on the platform alone. Read from the platform, so
        # the figure is its own, not one we keep.
        invoice = await self._provider.find_invoice(action.invoice_id)
        if invoice is not None and (
            action.amount.currency != invoice.total.currency
            or action.amount.amount_minor > invoice.total.amount_minor
        ):
            raise IllegalTransition(
                action_id,
                action.state,
                ActionState.EXECUTED,
                why=(
                    f"({action.amount} exceeds invoice {invoice.id}, which totals {invoice.total})"
                ),
            )

        # Every send falls within the platform's window from the first
        # attempt, read from the store -- a restart reads the same. Past it,
        # the platform may no longer answer the key: nothing is sent again.
        # A platform with no window keeps no key: a retry only ever follows
        # a refusal that made nothing, since an unknown outcome there is
        # never FAILED (spec 015, FR-007).
        window = self._provider.idempotency_window
        if (
            window is not None
            and action.first_attempt_at is not None
            and not self._within_window(action)
        ):
            self._actions.annotate(
                action_id,
                action.state,
                attention="window_passed",
                resolution=(
                    "not sent again: the platform may no longer answer this idempotency "
                    "key; left to a person"
                ),
            )
            raise NeedsAPerson(action_id, action.state, "window_passed")

        # Claim the write before making it. Of two concurrent executions, one
        # moves the action to EXECUTING and rings the provider; the other
        # finds it claimed and is refused. The guarantee is the store's
        # conditional UPDATE: transition() re-reads the state and only moves
        # it from the one it found, so whichever caller claims second fails,
        # in this process or another.
        # The first claim records when the write was first attempted, before
        # anything is sent; a later claim keeps it. The claim also refuses
        # while another credit note of the customer is in flight, unknown or
        # left half-way (spec 015, FR-009), in the same UPDATE.
        claimed_at = self._now()
        action = self._actions.transition(
            action_id, ActionState.EXECUTING, first_attempt_at=claimed_at
        )
        send_by = None if window is None else action.first_attempt_at + window
        stopped: list[str] = []
        before_request = self._before_request(action, claimed_at, stopped)

        try:
            # How the write lands on the invoice is planned once, before the
            # first send, and every retry sends exactly that.
            allocation = action.allocation
            if allocation is None:
                allocation = await self._provider.plan_write(action.invoice_id, action.action_type)
                self._actions.annotate(action_id, ActionState.EXECUTING, allocation=allocation)
            result = await write(
                invoice_id=action.invoice_id,
                amount_minor=action.amount.amount_minor,
                currency=action.amount.currency,
                reason=action.reason,
                idempotency_key=action.idempotency_key,
                action_id=action.action_id,
                allocation=allocation,
                send_by=send_by,
                breakdown=action.tax_breakdown,
                before_request=before_request,
            )
        except NotSent as error:
            if stopped:
                return self._stopped(action, stopped[0], error)
            # The deadline passed before the adapter could send: nothing left
            # by this attempt, and nothing will be sent again. After an
            # earlier unknown outcome it proves nothing about that one: the
            # outcome stays unknown, for a person's status read.
            settled = (
                ActionState.FAILED
                if action.uncertain_since is None
                else ActionState.UNKNOWN_OUTCOME
            )
            self._actions.transition(
                action_id,
                settled,
                failure_reason="not sent: the platform's window had passed",
                unless_incident=True,
            )
            self._actions.annotate(
                action_id,
                settled,
                attention="window_passed",
                resolution=(
                    "not sent: the platform may no longer answer this idempotency key; "
                    "left to a person"
                ),
            )
            raise NeedsAPerson(action_id, settled, "window_passed") from None
        except ProviderRejected as error:
            return await self._refused(action, error)
        except PartialWrite as error:
            # A document was made and not completed: final for everything
            # automatic, whatever the claim became meanwhile (FR-006).
            self._partial(
                action_id,
                error.document_id,
                error.stage,
                failure_reason=f"{type(error).__name__}: {error}",
            )
            raise NeedsAPerson(action_id, ActionState.UNKNOWN_OUTCOME, "partial_write") from error
        except Exception as error:  # noqa: BLE001 - recorded, then re-raised
            # No answer, or one we cannot read: the write may have landed.
            # An answer the adapter could not read is an incident besides.
            unreadable = getattr(error, "category", None) == "unreadable_answer"
            self._actions.transition(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason=f"{type(error).__name__}: {error}",
                uncertain_since=self._now(),
                **({"attention": "unreadable_answer"} if unreadable else {}),
            )
            raise

        # The platform's own answer is checked like a write found again:
        # recorded only if it is the write the person approved.
        problem = _unlike(action, allocation, result)
        if problem == "partial_write":
            self._partial(
                action_id,
                result.provider_action_id,
                result.partial,
                failure_reason="the platform's answer is a document not completed",
            )
            raise NeedsAPerson(action_id, ActionState.UNKNOWN_OUTCOME, problem)
        if problem is not None:
            self._actions.transition(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason=f"the platform's answer is not the write approved ({problem})",
                uncertain_since=self._now(),
                attention=problem,
            )
            raise NeedsAPerson(action_id, ActionState.UNKNOWN_OUTCOME, problem)
        return self._record(action_id, result, how="made as {ref}")

    def _before_request(self, action: PendingAction, claimed_at: datetime, stopped: list[str]):
        """The hook the write calls before each of its requests (FR-011).

        Past the step deadline (`STEP_DEADLINE` after the claim), or once the
        row is no longer `EXECUTING` under this claim's token, it raises
        `NotSent` and says why in `stopped`; otherwise it records the
        request's instant, durably, in the same conditional UPDATE that
        checks the claim. The adapter turns `NotSent` into `PartialWrite`
        once a document exists.
        """
        deadline = claimed_at + STEP_DEADLINE
        token = action.claim_token
        # Each request's own timeout is counted: one that could not end before
        # the deadline is not started (FR-011).
        timeout = getattr(self._provider, "request_timeout", timedelta(0))

        async def before_request() -> None:
            now = self._now()
            if now + timeout >= deadline:
                stopped.append("deadline")
                raise NotSent("step_deadline")
            if token is None or not self._actions.record_request(action.action_id, token, now):
                stopped.append("claim")
                raise NotSent("claim_lost")

        return before_request

    def _stopped(self, action: PendingAction, why: str, error: NotSent) -> PendingAction:
        """The attempt stopped itself before a request, with no document
        made (FR-011). A claim taken over is left to whoever holds the row
        now: nothing is recorded over it. Past the step deadline, nothing
        was made by this attempt: FAILED, retried by a fresh claim -- or, if
        an earlier attempt's outcome was unknown, still unknown."""
        if why == "claim":
            raise IllegalTransition(
                action.action_id,
                self._actions.get(action.action_id).state,
                ActionState.EXECUTED,
                why="(this attempt's claim is no longer held; it sent nothing more)",
            ) from error
        settled = (
            ActionState.FAILED if action.uncertain_since is None else ActionState.UNKNOWN_OUTCOME
        )
        self._actions.transition(
            action.action_id,
            settled,
            failure_reason="not sent: the attempt's step deadline had passed",
            unless_incident=True,
        )
        raise error

    def _partial(
        self,
        action_id: str,
        document_id: str,
        stage: str | None,
        failure_reason: str | None = None,
        how: str = "the write stopped",
    ) -> PendingAction:
        """A document made and not completed: `partial_write`, its id and
        stage recorded, the in-flight warning in the resolution (FR-006)."""
        return self._actions.mark_partial(
            action_id,
            attention="partial_write",
            partial_document={"document_id": document_id, "stage": stage},
            failure_reason=failure_reason,
            at=self._now(),
            resolution=(
                f"{how} after document {document_id} was made, left at stage {stage}; "
                f"left to a person -- {IN_FLIGHT}"
            ),
        )

    async def _refused(self, action: PendingAction, error: ProviderRejected) -> PendingAction:
        """The platform said no to this attempt.

        Nothing was created by it. If no earlier attempt of the action had an
        unknown outcome, the action is FAILED. Otherwise an earlier one may
        have landed -- a first answer lost, then a retry refused: the action
        is reconciled, and the refusal settles nothing on its own
        (specs/002-stripe-writes, FR-006).
        """
        if action.uncertain_since is None:
            self._actions.transition(
                action.action_id,
                ActionState.FAILED,
                failure_reason=str(error),
                unless_incident=True,
            )
            raise error
        self._actions.transition(
            action.action_id,
            ActionState.UNKNOWN_OUTCOME,
            failure_reason=f"refused ({error}) after an earlier attempt's outcome was unknown",
        )
        settled = await self.reconcile(action.action_id, refusal=str(error))
        if settled.state is ActionState.EXECUTED:
            return settled
        if settled.state is ActionState.FAILED:
            raise error
        raise NeedsAPerson(action.action_id, settled.state, settled.attention or "outcome_unknown")

    def _within_window(self, action: PendingAction) -> bool:
        """Whether the platform still answers the action's key with what it
        made: only when the first attempt is known, and recent enough. Never
        on a platform with no window: an absence there proves nothing."""
        window = self._provider.idempotency_window
        return (
            window is not None
            and action.first_attempt_at is not None
            and self._now() - action.first_attempt_at < window
        )

    def _record(
        self, action_id: str, made: ProviderAction, how: str, by_person: bool = False
    ) -> PendingAction:
        """The action EXECUTED with the platform's reference, and what the
        platform said of the write, dated. A status but success is put to a
        person; the write is never made again for it. Settled by the system,
        it is refused if an incident was recorded meanwhile; only a person's
        status read (`by_person`) settles one."""
        followed = made.status != "succeeded"
        made_how = how.format(ref=made.provider_action_id)
        resolution = (
            f"{made_how}; the platform says {made.status} "
            f"({made.observed_at:%Y-%m-%d %H:%M} UTC): to follow up"
            if followed
            else made_how
        )
        return self._actions.transition(
            action_id,
            ActionState.EXECUTED,
            provider_action_id=made.provider_action_id,
            resolution=resolution,
            provider_status=made.status,
            provider_status_at=made.observed_at,
            attention="not_successful" if followed else None,
            unless_incident=not by_person,
        )

    async def refresh_status(self, action_id: str) -> PendingAction:
        """A person has the platform asked again what became of the write.

        Nothing is ever sent: a read, which needs no writing, so it works
        with writes switched off. For a write made, the status observed is
        recorded with its instant. For an action an incident left to a
        person, the platform's answer settles it only when it holds exactly
        one write alike; anything else leaves it where it is, and never
        permits a new write (specs/002-stripe-writes, FR-003).
        """
        action = self._actions.get(action_id)
        left = action.attention in INCIDENTS and action.state in (
            ActionState.UNKNOWN_OUTCOME,
            ActionState.EXECUTING,
            ActionState.FAILED,
        )
        if action.state is not ActionState.EXECUTED and not left:
            raise IllegalTransition(
                action_id,
                action.state,
                ActionState.EXECUTED,
                why="(only a write made, or an action left to a person, has a status to read)",
            )
        # Dated when the read starts, not when it returns: a read begun too
        # soon after the attempt's last request never counts as a late one,
        # however long the platform took to answer (FR-009a).
        started = self._now()
        found = await self._provider.find_action(action.idempotency_key, action.invoice_id)
        # Every read is recorded, nothing found included: a resolution needs
        # one made long enough after the attempt's last request.
        action = self._actions.record_status_read(action_id, started, _summary(found))
        if action.state is ActionState.EXECUTED:
            return self._observed(action, found)
        return self._settled_by_a_person(action, found)

    def resolve(self, action_id: str, resolved_by: str, note: str) -> PendingAction:
        """A person records how they resolved a partial write, or a write
        that left no trace (spec 015, FR-009a).

        Only on those two, only once, only after a status read made at least
        ten minutes after the attempt's last request -- the store checks all
        three in one conditional UPDATE. It releases the customer's blocker;
        the action stays final for everything automatic and is never
        executed again. Nothing is read or sent.
        """
        text = note.strip()
        if not text or len(text) > RESOLUTION_NOTE_LIMIT:
            raise ValueError(
                f"a resolution says what was done, in 1 to {RESOLUTION_NOTE_LIMIT} characters"
            )
        return self._actions.record_resolution(action_id, resolved_by, text, self._now())

    def _observed(self, action: PendingAction, found: list[ProviderAction]) -> PendingAction:
        # Every write under the key counts: a second one beside the write
        # recorded is an incident, never hidden by looking at ours alone.
        if len(found) > 1:
            return self._actions.annotate(
                action.action_id,
                ActionState.EXECUTED,
                attention="several_writes",
                resolution=(
                    f"status read: the platform holds {len(found)} writes under this "
                    f"idempotency key; left to a person"
                ),
            )
        mine = [made for made in found if made.provider_action_id == action.provider_action_id]
        if len(mine) != 1:
            return self._actions.annotate(
                action.action_id,
                ActionState.EXECUTED,
                attention="unlike_the_action",
                resolution=(
                    f"status read: the platform no longer shows {action.provider_action_id} "
                    f"alone under this idempotency key; left to a person"
                ),
            )
        (made,) = mine
        if made.partial is not None:
            return self._partial(
                action.action_id,
                made.provider_action_id,
                made.partial,
                how="status read: the platform shows the write not completed",
            )
        problem = _unlike(action, action.allocation, made)
        if problem is not None:
            return self._actions.annotate(
                action.action_id,
                ActionState.EXECUTED,
                attention=problem,
                resolution=f"status read: {made.provider_action_id} is not the write approved",
            )
        followed = made.status != "succeeded"
        return self._actions.annotate(
            action.action_id,
            ActionState.EXECUTED,
            attention="not_successful" if followed else None,
            provider_status=made.status,
            provider_status_at=made.observed_at,
            resolution=(
                f"status read: the platform says {made.status} "
                f"({made.observed_at:%Y-%m-%d %H:%M} UTC)" + (": to follow up" if followed else "")
            ),
        )

    def _settled_by_a_person(
        self, action: PendingAction, found: list[ProviderAction]
    ) -> PendingAction:
        if len(found) == 1:
            (made,) = found
            if made.partial is not None:
                # A document found half-way is never success (FR-008).
                return self._partial(
                    action.action_id,
                    made.provider_action_id,
                    made.partial,
                    how="status read: the platform holds the write not completed",
                )
            problem = _unlike(action, action.allocation, made)
            if problem is None:
                return self._record(
                    action.action_id,
                    made,
                    how="found by a person's status read: {ref}",
                    by_person=True,
                )
            return self._actions.annotate(
                action.action_id,
                action.state,
                attention=problem,
                resolution=f"status read: {made.provider_action_id} is not the write approved",
            )
        if found:
            return self._actions.annotate(
                action.action_id,
                action.state,
                attention="several_writes",
                resolution=(
                    f"status read: the platform holds {len(found)} writes under this "
                    f"idempotency key; left to a person"
                ),
            )
        return self._actions.annotate(
            action.action_id,
            action.state,
            resolution="status read: nothing under this idempotency key; left to a person",
        )

    async def reconcile(
        self,
        action_id: str,
        stale_after: timedelta | None = None,
        refusal: str | None = None,
    ) -> PendingAction:
        """Settle an action whose outcome nobody recorded.

        Two situations reach here: the platform did not answer
        (UNKNOWN_OUTCOME), or a claim was left in EXECUTING for longer than
        ``stale_after`` by a process that died before writing the answer
        down -- itself an unknown outcome. Either way the platform is asked
        what it holds under the idempotency key on the invoice:

        - one write, alike: EXECUTED with its reference and status;
        - one unlike the action, or several: left to a person;
        - one, with no allocation planned for it: left to a person;
        - none, within the window: FAILED, which ``execute`` retries under
          the same key;
        - none past the window: left to a person -- an absence the platform
          can no longer vouch for proves nothing.

        An action with an incident is refused before anything is read: it is
        a person's to settle (`refresh_status`). It never writes. Anything
        else is returned unchanged. `refusal` is what the platform answered
        the latest attempt, kept as the reason when nothing is found.
        """
        action = self._actions.get(action_id)
        self._refuse_unwritten(action)
        # An incident is never settled automatically, not even by a write
        # alike: only a person's status read does that. Nothing is read.
        if action.attention in INCIDENTS:
            raise NeedsAPerson(action_id, action.state, action.attention)
        stale_after = self._stale_after if stale_after is None else stale_after
        abandoned = (
            action.state is ActionState.EXECUTING
            and action.updated_at is not None
            and datetime.now(UTC) - action.updated_at >= stale_after
        )
        if action.state is not ActionState.UNKNOWN_OUTCOME and not abandoned:
            return action
        if abandoned:
            action = self._actions.transition(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason="the claim was abandoned before its outcome was recorded",
                uncertain_since=self._now(),
            )

        found = await self._provider.find_action(action.idempotency_key, action.invoice_id)
        if len(found) > 1:
            # Two writes under one key: an incident for a person, never "the
            # first one found". Nothing is written, and nothing settled.
            return self._actions.annotate(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                attention="several_writes",
                resolution=(
                    f"reconciled: the platform holds {len(found)} writes under this "
                    f"idempotency key; left to a person"
                ),
            )
        if found:
            (made,) = found
            if made.partial is not None:
                # A document made and not completed: never success, never
                # resumed (FR-006, FR-008).
                return self._partial(
                    action_id,
                    made.provider_action_id,
                    made.partial,
                    how="reconciled: the platform holds the write not completed",
                )
            if action.allocation is None:
                return self._actions.annotate(
                    action_id,
                    ActionState.UNKNOWN_OUTCOME,
                    attention="window_unknown",
                    resolution=(
                        f"reconciled: {made.provider_action_id} is held under this "
                        f"idempotency key, but no allocation was planned for it; left to a person"
                    ),
                )
            problem = _unlike(action, action.allocation, made)
            if problem is not None:
                return self._actions.annotate(
                    action_id,
                    ActionState.UNKNOWN_OUTCOME,
                    attention=problem,
                    resolution=(
                        f"reconciled: {made.provider_action_id} is held under this "
                        f"idempotency key and is not the write approved; left to a person"
                    ),
                )
            return self._record(
                action_id,
                made,
                how="reconciled: the platform already held {ref} under this idempotency key; "
                "no second write",
            )
        if self._provider.idempotency_window is None:
            # No key kept: an absence is never proof. The creation may still
            # land; a person's status read can settle it then (FR-007).
            return self._actions.mark_partial(
                action_id,
                attention="no_trace",
                at=self._now(),
                resolution=(
                    "reconciled: nothing found under this idempotency key, which this "
                    f"platform does not keep; never sent again, left to a person -- {IN_FLIGHT}"
                ),
            )
        if not self._within_window(action):
            return self._actions.annotate(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                attention="window_passed",
                resolution=(
                    "reconciled: nothing under this idempotency key, but the platform may no "
                    "longer answer it; left to a person"
                ),
            )
        return self._actions.transition(
            action_id,
            ActionState.FAILED,
            failure_reason=(
                f"refused ({refusal}); no earlier write found on the platform"
                if refusal
                else "no write found on the platform after an unknown outcome"
            ),
            unless_incident=True,
            resolution="reconciled: nothing held under this idempotency key; safe to retry",
        )


def _unlike(action: PendingAction, allocation: str | None, made: ProviderAction) -> str | None:
    """Why a write the platform holds is not the one approved, or None.

    Every field the action fixed is compared; a write that does not say how
    it was allocated, or what became of it, cannot be read. With no
    allocation planned (an action older than the plan, or one whose plan
    never completed), the write's must be one its type allows.
    """
    if made.partial is not None:
        # A document not completed is never the write approved (FR-008).
        return "partial_write"
    if made.allocation is None or made.status is None:
        return "unreadable_answer"
    if (
        made.invoice_id != action.invoice_id
        or made.action_type != action.action_type
        or made.amount_minor != action.amount.amount_minor
        or made.currency != action.amount.currency
        or not made.test_mode
        or made.idempotency_key != action.idempotency_key
        or (made.action_id is not None and made.action_id != action.action_id)
    ):
        return "unlike_the_action"
    if allocation is not None and made.allocation != allocation:
        return "unlike_the_action"
    if made.allocation not in ALLOCATIONS.get(action.action_type, frozenset()):
        return "unlike_the_action"
    # A platform that builds the credit note from new lines reports what it
    # made of them: compared exactly with the breakdown approved (FR-010).
    # Platforms that report none, and refunds, are compared as above.
    if (
        action.action_type == "credit_note"
        and (made.breakdown_reported or made.breakdown is not None)
        and _normalised(made.breakdown) != _normalised(action.tax_breakdown)
    ):
        return "unlike_the_action"
    return None


def _normalised(breakdown: TaxBreakdown | None) -> TaxBreakdown | None:
    """A breakdown whose every tax is zero is an untaxed amount: None."""
    if breakdown is None or all(amount.amount_minor == 0 for _, amount in breakdown.taxes):
        return None
    return breakdown


def _summary(found: list[ProviderAction]) -> str:
    """What a status read found, in words, for the record."""
    if not found:
        return "nothing under this idempotency key"
    if len(found) > 1:
        return f"{len(found)} writes under this idempotency key"
    (made,) = found
    if made.partial is not None:
        return f"{made.provider_action_id}, not completed ({made.partial})"
    return f"{made.provider_action_id}, {made.status}"

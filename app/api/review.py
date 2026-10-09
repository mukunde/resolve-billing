"""The page a billing manager actually sees.

The email is a notification; this is where the decision is made. That split is
what every finance approval system does -- Ariba, Coupa, Workday -- and for
three reasons, in order:

**Identity.** A controller has to be able to prove *who* approved. An
anonymous link cannot: whoever holds it acts. Non-repudiation needs
authentication.

**Email is untrusted transport.** It gets forwarded, quoted, archived, and
left in shared mailboxes. Putting the power to move money in a channel you do
not control hands a signature to a stranger.

**A GET link is prefetched.** Outlook Safe Links and antivirus gateways follow
links to scan them, so a one-click approve URL can be exercised by a scanner
rather than a person. The decision here arrives by POST, from a form a human
submitted.

The identity recorded against a decision is the authenticated approver's,
never a name typed into the form (ADR-009). In this demonstrator a person signs
in with HTTP Basic against a configured list; in production this route sits
behind the company's SSO, and the identity comes from there.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse

from app.api.auth import Approver
from app.api.deps import get_actions, get_approvals, get_audit
from app.api.templating import render as _page
from app.application.execute_action import ApprovalService
from app.domain.enums import ActionState
from app.domain.money import Money
from app.infrastructure.actions import (
    IllegalTransition,
    PendingAction,
    SqliteActionStore,
    UnknownAction,
)
from app.infrastructure.audit import SqliteAuditRepository

router = APIRouter(prefix="/api/v1/actions", tags=["review"])

RESUME_TIMEOUT_SECONDS = 10


@router.get("/{action_id}/review", response_class=HTMLResponse)
async def review_page(
    action_id: str,
    approver: Approver,
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
    audit: Annotated[SqliteAuditRepository, Depends(get_audit)],
    resume: str | None = None,
) -> HTMLResponse:
    """Show the decision, and the two buttons.

    ``resume`` is the orchestrator's callback, handed over on the first visit
    and stored: the reviewer can reload the page or come back tomorrow without
    carrying it in the URL.
    """
    action = _found(lambda: actions.get(action_id))

    # Kept only if it is the orchestrator's own callback, rebuilt here from
    # the configured origin. Anything else is ignored: the decision still
    # counts, nobody is called back. ADR-009, D8.
    callback = orchestrator_callback(resume) if resume else None
    if callback:
        actions.set_resume_url(action_id, callback)

    # Past its deadline the request is expired here, and shown as such.
    action = approvals.expire_if_overdue(action_id)
    if action.state is not ActionState.PENDING_APPROVAL:
        return HTMLResponse(_already_decided(action), status_code=409)

    run = audit.run(action.run_id)
    calibrated = run is not None and run["confidence_kind"] == "calibrated"

    return HTMLResponse(
        _page(
            "review.html",
            post_url=f"/api/v1/actions/{action.action_id}/review",
            approver=approver.identity,
            run_id=action.run_id,
            action_id=action.action_id,
            invoice_id=action.invoice_id,
            action_type=action.action_type,
            amount_display=_money(action.amount),
            classification=run["classification"] if run else "—",
            findings=_findings(run),
            engine=(
                f"{run['decision_engine']} ({run['decision_engine_version']})"
                if run
                else "—"
            ),
            confidence=(
                f"{run['confidence']} · {run['confidence_kind']}" if run else "—"
            ),
            badge_bg="#dcfce7" if calibrated else "#fef3c7",
            badge_fg="#166534" if calibrated else "#92400e",
            caveat=(
                "Score mesuré sur le jeu de cas de référence."
                if calibrated
                else "Score déclaré par le modèle, non mesuré. Les seuils "
                "automatiques restent désactivés pour cette raison."
            ),
            reason=action.reason,
            policy=run["authorization_reason"] if run else "",
            # What the approver is shown, as a value the decision carries
            # back: an approval binds to this proposal on this invoice. D13.
            proposal=await approvals.proposal_fingerprint(action),
            deadline=_deadline(action),
        )
    )


@router.post("/{action_id}/review", response_class=HTMLResponse)
async def submit_review(
    action_id: str,
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
    decision: Annotated[str, Form()],
    approver: Approver,
    proposal: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Record the human decision, then tell the orchestrator to carry on.

    Approve records *who*; it does not execute. The orchestrator resumes and
    calls execute, which keeps "who authorised this" and "did it go through"
    as two separate questions for the audit -- and means a provider outage
    never costs a second human decision.
    """
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "decision must be 'approve' or 'reject'")
    if decision == "approve" and not proposal:
        raise HTTPException(422, "an approval names the proposal it approves")

    action = _found(lambda: actions.get(action_id))
    try:
        if decision == "approve":
            action = await approvals.approve(action_id, approver.identity, proposal)
        else:
            action = approvals.reject(action_id, approver.identity)
    except IllegalTransition:
        return HTMLResponse(_already_decided(actions.get(action_id)), status_code=409)

    resumed = _resume(action, decision, approver.identity)
    return HTMLResponse(_decided(action, decision, resumed))


# -- rendering -------------------------------------------------------------


def _decided(action: PendingAction, decision: str, resumed: bool) -> str:
    approved = decision == "approve"
    note = (
        "L'orchestrateur a été notifié et exécute l'action."
        if resumed
        else "L'orchestrateur n'a pas pu être joint. La décision est enregistrée "
        "et l'action reste reprenable : rien n'est perdu."
    )
    return _page(
        "review_done.html",
        headline="Action approuvée" if approved else "Action refusée",
        subhead=(
            "Aucun avoir n'est émis tant que l'exécution n'a pas abouti."
            if approved
            else "Aucun mouvement financier n'aura lieu."
        ),
        banner_bg="#00695C" if approved else "#C2185B",
        banner_fg="#ffffff",
        action_id=action.action_id,
        state=action.state.value,
        amount_display=_money(action.amount),
        decided_by=action.approved_by or "—",
        provider_action_id=action.provider_action_id or "—",
        footnote=note,
    )


def _already_decided(action: PendingAction) -> str:
    """Reopening a link for something already settled is not an error worth
    a stack trace -- it is a reviewer double-checking, or a second approver
    arriving late. Show them the outcome."""
    return _page(
        "review_done.html",
        headline="Décision déjà enregistrée",
        subhead=f"Cette action est à l'état « {action.state.value} ».",
        banner_bg="#475569",
        banner_fg="#ffffff",
        action_id=action.action_id,
        state=action.state.value,
        amount_display=_money(action.amount),
        decided_by=action.approved_by or "—",
        provider_action_id=action.provider_action_id or "—",
        footnote=(
            "Délai de décision dépassé, ou la facture a changé depuis que la "
            "proposition a été présentée : le litige est à réexaminer."
            if action.state is ActionState.EXPIRED
            else "Un lien de validation ne vaut que pour une décision."
        ),
    )


def _deadline(action: PendingAction) -> str:
    if action.expires_at is None:
        return "—"
    return f"{action.expires_at:%d/%m/%Y %H:%M} UTC"


def _money(amount: Money) -> str:
    return f"{amount.to_decimal():,.2f} {amount.currency}".replace(",", " ")


def _findings(run) -> str:
    if run is None:
        return "—"
    codes = [finding["code"] for finding in json.loads(run["findings_json"])]
    return ", ".join(codes) or "aucun"


# -- orchestrator callback -------------------------------------------------

#: The only callback shape accepted: n8n's waiting webhook for one execution.
_WAITING_PATH = re.compile(r"/webhook-waiting/(\d+)")
_SIGNATURE = re.compile(r"[0-9A-Fa-f]{1,128}")


def orchestrator_callback(resume: str) -> str | None:
    """The callback to ring, rebuilt from the configured origin, or None.

    The `resume` URL reaches this service through a browser, so it is
    untrusted input, and calling it as given would let anyone who can put a
    link in front of a reviewer make this server request any address it can
    reach: a cloud metadata endpoint, an internal service. So it is only
    accepted when it is the orchestrator's own waiting webhook, on the origin
    configured in ``RESOLVE_ORCHESTRATOR_URL`` -- same scheme, host and port,
    the path ``/webhook-waiting/<execution id>``, and nothing in the query
    but n8n's signature. The URL rung is then built here, from those parts,
    never from the string as received. Not configured means no callback.

    The orchestrator's resume URL is minimal by design (ADR-010 removes it):
    a durable workflow takes the decision as a signal instead.
    """
    base = os.getenv("RESOLVE_ORCHESTRATOR_URL")
    if not base:
        return None
    origin = urllib.parse.urlsplit(base)
    given = urllib.parse.urlsplit(resume)
    if origin.scheme not in ("http", "https") or not origin.netloc:
        return None
    if (given.scheme, given.netloc) != (origin.scheme, origin.netloc):
        return None
    waiting = _WAITING_PATH.fullmatch(given.path)
    query = urllib.parse.parse_qs(given.query, keep_blank_values=True)
    if waiting is None or set(query) != {"signature"} or len(query["signature"]) != 1:
        return None
    signature = query["signature"][0]
    if not _SIGNATURE.fullmatch(signature):
        return None
    return (
        f"{origin.scheme}://{origin.netloc}/webhook-waiting/{waiting.group(1)}"
        f"?signature={signature}"
    )


def _resume(action: PendingAction, decision: str, by: str) -> bool:
    """Ring the orchestrator back. Best effort, and deliberately so.

    The decision is already committed here. If n8n is down, redeployed or gone
    entirely, the action sits in a state the state machine can resume from --
    the human's click is never lost because the orchestrator was unavailable.
    That is the whole reason the state does not live in n8n.

    It is a GET, which n8n's waiting webhook is what accepts. The URL does
    reach a browser -- n8n hands it over inside the review link -- which is
    why it is never called as received: only the orchestrator's own waiting
    webhook, rebuilt from the configured origin, is rung
    (:func:`orchestrator_callback`, D8). Decisions on the page above still
    arrive by POST.

    And **the callback carries no authority**. Replaying it
    cannot approve anything: execution requires the action to already be in
    APPROVED, which only a recorded human decision produces. A leaked resume
    URL can at most re-trigger what a person already authorised.
    """
    # Checked again at call time: a row stored before this check existed
    # may hold any URL at all.
    callback = orchestrator_callback(action.resume_url) if action.resume_url else None
    if not callback:
        return False

    separator = "&" if "?" in callback else "?"
    query = urllib.parse.urlencode(
        {"decision": decision, "by": by, "action_id": action.action_id}
    )
    try:
        with urllib.request.urlopen(
            f"{callback}{separator}{query}", timeout=RESUME_TIMEOUT_SECONDS
        ):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _found(call):
    try:
        return call()
    except UnknownAction as error:
        raise HTTPException(404, str(error)) from error

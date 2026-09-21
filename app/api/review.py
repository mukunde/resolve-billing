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

The honest limit, stated on the page itself: the identifier field is not
authentication. In production this route sits behind the company's SSO and the
identity comes from there.
"""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from string import Template
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse

from app.api.deps import get_actions, get_approvals, get_audit
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

TEMPLATES = Path(__file__).parent / "templates"
RESUME_TIMEOUT_SECONDS = 10


def _page(name: str, **fields) -> str:
    """Render a template, escaping every value that came from data.

    An approval page vulnerable to injected markup would be a costly irony on
    a system whose threat model opens with "the dispute message is untrusted".
    """
    template = Template((TEMPLATES / name).read_text(encoding="utf-8"))
    return template.safe_substitute(
        {key: html.escape(str(value), quote=True) for key, value in fields.items()}
    )


@router.get("/{action_id}/review", response_class=HTMLResponse)
async def review_page(
    action_id: str,
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
    audit: Annotated[SqliteAuditRepository, Depends(get_audit)],
    resume: str | None = None,
) -> HTMLResponse:
    """Show the decision, and the two buttons.

    ``resume`` is the orchestrator's callback, handed over on the first visit
    and stored: the reviewer can reload the page or come back tomorrow without
    carrying it in the URL.
    """
    action = _found(lambda: actions.get(action_id))

    if resume:
        action = actions.set_resume_url(action_id, resume)

    if action.state is not ActionState.PENDING_APPROVAL:
        return HTMLResponse(_already_decided(action), status_code=409)

    run = audit.run(action.run_id)
    calibrated = run is not None and run["confidence_kind"] == "calibrated"

    return HTMLResponse(
        _page(
            "review.html",
            post_url=f"/api/v1/actions/{action.action_id}/review",
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
        )
    )


@router.post("/{action_id}/review", response_class=HTMLResponse)
async def submit_review(
    action_id: str,
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
    decision: Annotated[str, Form()],
    by: Annotated[str, Form(min_length=1, max_length=120)],
) -> HTMLResponse:
    """Record the human decision, then tell the orchestrator to carry on.

    Approve records *who*; it does not execute. The orchestrator resumes and
    calls execute, which keeps "who authorised this" and "did it go through"
    as two separate questions for the audit -- and means a provider outage
    never costs a second human decision.
    """
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "decision must be 'approve' or 'reject'")

    action = _found(lambda: actions.get(action_id))
    try:
        if decision == "approve":
            action = approvals.approve(action_id, by)
        else:
            action = approvals.reject(action_id, by)
    except IllegalTransition:
        return HTMLResponse(_already_decided(actions.get(action_id)), status_code=409)

    resumed = _resume(action, decision, by)
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
        footnote="Un lien de validation ne vaut que pour une décision.",
    )


def _money(amount: Money) -> str:
    return f"{amount.to_decimal():,.2f} {amount.currency}".replace(",", " ")


def _findings(run) -> str:
    if run is None:
        return "—"
    codes = [finding["code"] for finding in json.loads(run["findings_json"])]
    return ", ".join(codes) or "aucun"


# -- orchestrator callback -------------------------------------------------


def _resume(action: PendingAction, decision: str, by: str) -> bool:
    """Ring the orchestrator back. Best effort, and deliberately so.

    The decision is already committed here. If n8n is down, redeployed or gone
    entirely, the action sits in a state the state machine can resume from --
    the human's click is never lost because the orchestrator was unavailable.
    That is the whole reason the state does not live in n8n.

    It is a GET, which n8n's waiting webhook is what accepts. That is not the
    hole it looks like: this URL never reaches a human or a mail scanner. It
    is minted by the orchestrator, handed to this service, and kept in the
    database -- the link a reviewer clicks is the page above, and decisions
    there arrive by POST.

    More to the point, **the callback carries no authority**. Replaying it
    cannot approve anything: execution requires the action to already be in
    APPROVED, which only a recorded human decision produces. A leaked resume
    URL can at most re-trigger what a person already authorised.
    """
    if not action.resume_url:
        return False

    separator = "&" if "?" in action.resume_url else "?"
    query = urllib.parse.urlencode(
        {"decision": decision, "by": by, "action_id": action.action_id}
    )
    try:
        with urllib.request.urlopen(
            f"{action.resume_url}{separator}{query}", timeout=RESUME_TIMEOUT_SECONDS
        ):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _found(call):
    try:
        return call()
    except UnknownAction as error:
        raise HTTPException(404, str(error)) from error

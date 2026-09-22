"""A read-only page for a run that has nothing to approve.

Not every run that needs a human needs a *decision*. A dispute carrying an
injected instruction is stopped before the engine is consulted, so no action
is ever queued: there is nothing to authorise, only something to read. Sending
that to the approval page would offer two buttons over an action that does not
exist, and routing it nowhere would mean an attempted injection is detected,
recorded, and told to no one.

So it gets its own page, and the difference is the point. One page ends in a
decision; this one ends in a trace.

The trace is what makes it worth looking at. Events are collected as the run
proceeds rather than derived from its outcome, so `decision_engine_skipped`
with no `decision_created` after it is a positive record that the model was
never asked -- not a reconstruction, and not the same claim as the model
having declined.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.api.deps import get_audit
from app.api.templating import Safe, escape, render
from app.infrastructure.audit import SqliteAuditRepository

router = APIRouter(prefix="/api/v1/runs", tags=["review"])

#: Events that carry the reason a run was stopped before the engine. Marked in
#: the trace, because on this page they are the headline rather than a step.
BLOCKING_EVENTS = {"injection_suspected", "decision_engine_skipped"}


@router.get("/{run_id}/view", response_class=HTMLResponse)
async def run_page(
    run_id: str,
    audit: Annotated[SqliteAuditRepository, Depends(get_audit)],
) -> HTMLResponse:
    """Show a run, its findings and its trace. No buttons."""
    run = audit.run(run_id)
    if run is None:
        raise HTTPException(404, f"unknown run {run_id!r}")

    blocked = run["short_circuited_by"]
    events = audit.events(run_id)

    return HTMLResponse(
        render(
            "run.html",
            headline=(
                "Revue humaine requise" if blocked else f"Run {run_id}"
            ),
            subhead=(
                f"Arrêté avant le moteur de décision par « {blocked} ». "
                "Aucune action n'a été mise en file : il n'y a rien à approuver."
                if blocked
                else "Consultation d'un run et de sa trace."
            ),
            banner_bg="#7f1d1d" if blocked else "#1f2937",
            banner_fg="#ffffff",
            kind_bg="#dcfce7" if run["confidence_kind"] == "calibrated" else "#fef3c7",
            kind_fg="#166534" if run["confidence_kind"] == "calibrated" else "#92400e",
            run_id=run["run_id"],
            created_at=run["created_at"],
            classification=run["classification"],
            confidence=f"{run['confidence']:.2f}",
            confidence_kind=run["confidence_kind"],
            engine=_engine(run),
            findings=_findings(run),
            verdict=run["verdict"],
            authorization_reason=run["authorization_reason"] or "—",
            explanation=run["explanation"] or "—",
            trace=_trace(events),
            absent_note=_absent_note(events),
            cost_display=_cost(run),
            latency_display=(
                f"{run['latency_ms']} ms" if run["latency_ms"] else "aucun appel"
            ),
            pricing_as_of=run["pricing_as_of"] or "sans objet",
        )
    )


# -- rendering helpers -----------------------------------------------------
#
# Each returns Safe, so each is responsible for escaping what came from the
# database. They take rows and hand back markup; nothing else builds markup.


def _engine(run) -> str:
    if run["decision_engine"] in (None, "none"):
        return "aucun — court-circuité par une règle déterministe"
    return f"{run['decision_engine']} ({run['decision_engine_version']})"


def _findings(run) -> str:
    codes = [finding["code"] for finding in json.loads(run["findings_json"])]
    return ", ".join(codes) or "aucun"


def _cost(run) -> str:
    """`None` and `0` are different answers, and the page must not merge them.

    An unknown rate and a model with no per-call price would otherwise both
    print as free -- one being a gap in the price table, the other a result.
    """
    if run["cost_micros"] is None:
        return "aucun — le moteur n'a pas été appelé"
    return f"${run['cost_micros'] / 1e6:.5f}"


def _trace(events) -> Safe:
    rows = []
    for event in events:
        payload = event["payload_json"] or ""
        css = " class='blocking'" if event["event"] in BLOCKING_EVENTS else ""
        rows.append(
            f"<li{css}>"
            f"<span class='seq'>{escape(event['sequence'])}</span>"
            f"<span class='name'>{escape(event['event'])}</span>"
            f"<span class='payload'>{escape(payload)}</span>"
            f"</li>"
        )
    return Safe("".join(rows) or "<li><span class='name'>aucun événement</span></li>")


def _absent_note(events) -> Safe:
    """Say out loud what is missing, because absence is the evidence here.

    A reader who does not know the event vocabulary cannot notice that
    `decision_created` never appears. Naming it turns a gap in a list into a
    statement someone can check.
    """
    names = {event["event"] for event in events}
    missing = [
        label
        for name, label in (
            ("decision_created", "<code>decision_created</code> — le moteur n'a jamais répondu"),
            ("action_queued", "<code>action_queued</code> — aucune action n'attend d'approbation"),
        )
        if name not in names
    ]
    if not missing:
        return Safe("")
    return Safe(
        "<p class='absent'>Absent de cette trace : " + " ; ".join(missing) + ".</p>"
    )

"""FastAPI entry point."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import approvals, disputes, health, review, run_view
from app.observability import tracing

app = FastAPI(
    title="ResolveBilling",
    description=(
        "Billing dispute investigation: deterministic checks first, a language "
        "model reads the complaint, code computes correction amounts, and a "
        "person approves every financial action."
    ),
    version="0.1.0",
)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    """Say what was wrong, never what was sent (ADR-009).

    FastAPI's default 422 echoes each invalid value back -- a message past the
    length limit came back whole, and the workflow mailed the error to a
    person. The location and the reason are enough to fix a request.
    """
    detail = [
        {
            "loc": list(problem.get("loc", ())),
            "type": problem.get("type"),
            "msg": problem.get("msg"),
        }
        for problem in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})

# No endpoint configured means no exporter, no provider, and a code path
# identical to the one that ran before tracing existed. See app/observability.
tracing.configure()

app.include_router(health.router)
app.include_router(disputes.router)
app.include_router(approvals.router)
app.include_router(review.router)
app.include_router(run_view.router)

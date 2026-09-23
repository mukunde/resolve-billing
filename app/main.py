"""FastAPI entry point."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import approvals, disputes, health, review, run_view
from app.observability import tracing

app = FastAPI(
    title="Agentic Billing Exception Handler",
    description=(
        "Provider- and model-agnostic agentic workflow for investigating "
        "billing disputes and safely recommending corrective actions."
    ),
    version="0.1.0",
)

# No endpoint configured means no exporter, no provider, and a code path
# identical to the one that ran before tracing existed. See app/observability.
tracing.configure()

app.include_router(health.router)
app.include_router(disputes.router)
app.include_router(approvals.router)
app.include_router(review.router)
app.include_router(run_view.router)

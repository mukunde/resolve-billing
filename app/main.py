"""FastAPI entry point."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import approvals, disputes, health

app = FastAPI(
    title="Agentic Billing Exception Handler",
    description=(
        "Provider- and model-agnostic agentic workflow for investigating "
        "billing disputes and safely recommending corrective actions."
    ),
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(disputes.router)
app.include_router(approvals.router)

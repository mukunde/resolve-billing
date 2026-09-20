"""Liveness, and which implementations are actually wired in."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import get_engine, get_provider

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Names the concrete provider and engine behind the two ports.

    Useful in the demo: it makes the swappable pieces visible without reading
    any code.
    """
    engine = get_engine()
    return {
        "status": "ok",
        "billing_provider": get_provider().name,
        "decision_engine": engine.name,
        "decision_engine_version": engine.version,
    }

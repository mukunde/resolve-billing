"""Liveness, and which implementations are actually wired in."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import allowed_engines, allowed_models, get_engine, get_provider

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict:
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
        # Shown so the demo can point at what a caller may ask for without
        # reading any code -- and at what it may not.
        "allowed_engines": allowed_engines(),
        "allowed_models": allowed_models(),
    }

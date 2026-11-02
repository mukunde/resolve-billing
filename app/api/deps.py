"""Wiring. The only module that names a concrete provider or engine.

Everything else in ``app/`` depends on the two protocols. Swapping the billing
platform or the decision engine is an edit here and nowhere else -- which is
the claim of ADR-001 and ADR-002, made checkable.
"""

from __future__ import annotations

import importlib.util
import os
from functools import lru_cache

from app.application.execute_action import ApprovalService
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import DecisionEngine
from app.decision.bounded import BoundedEngine
from app.decision.claude import ClaudeDecisionEngine
from app.decision.hosted import configuration
from app.decision.mock import MockDecisionEngine
from app.infrastructure.actions import SqliteActionStore
from app.infrastructure.attempts import SqliteAttemptStore
from app.infrastructure.audit import SqliteAuditRepository
from app.providers.base import BillingProvider
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.stripe.adapter import StripeBillingProvider
from app.providers.stripe.client import connect, connect_writer

#: The engine ADR-013 retained provisionally, on synthetic data only.
LUNA = "gpt-6-luna"


def _luna() -> DecisionEngine:
    """The configuration spec 008 measured, under the service's bounds."""
    from app.decision.openai_compatible import OpenAICompatibleEngine

    return BoundedEngine(OpenAICompatibleEngine(configuration(LUNA)), get_attempts())


#: Engines that exist today. The roadmap ones are absent on purpose: an empty
#: stub in this table would suggest an integration that is not there.
ENGINES: dict[str, type] = {
    "mock": MockDecisionEngine,
    "claude": ClaudeDecisionEngine,
    LUNA: _luna,
}


#: Billing platforms that exist today. The fake stays the default.
PROVIDERS = ("fake", "stripe")


@lru_cache(maxsize=1)
def get_provider() -> BillingProvider:
    name = os.getenv("BILLING_PROVIDER", "fake").lower()
    if name == "fake":
        return FakeBillingProvider()
    if name == "stripe":
        # The restricted test key, read here and nowhere else, and checked
        # before anything else is done (FR-021, FR-024). Stripe reads only
        # unless writes are switched on, by this exact word and nowhere else
        # (specs/002-stripe-writes, FR-002).
        key = os.getenv("STRIPE_API_KEY")
        reader = connect(key)
        writer = connect_writer(key) if os.getenv("STRIPE_WRITES") == "enabled" else None
        return StripeBillingProvider(reader, writer=writer)
    raise RuntimeError(f"unknown billing provider {name!r}; available: {list(PROVIDERS)}")


def selection_refusal() -> str | None:
    """Why the selected engine may not start, or None (spec 009, FR-009).

    A safeguard against a slip in configuration, not a control of content: the
    service cannot tell a real message from a synthetic one, so the declaration
    is the operator's. Lifting it is a decision of its own, never a variable.
    """
    if os.getenv("DECISION_ENGINE", "mock").lower() != LUNA:
        return None
    if os.getenv("BILLING_PROVIDER", "fake").lower() != "fake":
        return f"{LUNA} runs on the fake billing platform only (BILLING_PROVIDER=fake)"
    if os.getenv("RESOLVE_DATA") != "synthetic":
        return f"{LUNA} needs the data declared synthetic (RESOLVE_DATA=synthetic)"
    if importlib.util.find_spec("openai") is None:
        return f"{LUNA} needs the `engines` extra"
    config = configuration(LUNA)
    missing = [
        name
        for name in (config.key_variable, *config.account_variables)
        if not os.environ.get(name)
    ]
    if missing:
        return f"{LUNA} needs {', '.join(missing)}"
    from app.decision.openai_compatible import ambient_refusal

    return ambient_refusal()


@lru_cache(maxsize=1)
def get_engine() -> DecisionEngine:
    name = os.getenv("DECISION_ENGINE", "mock").lower()
    engine = ENGINES.get(name)
    if engine is None:
        raise RuntimeError(
            f"unknown decision engine {name!r}; available: {sorted(ENGINES)}"
        )
    if (refusal := selection_refusal()) is not None:
        raise RuntimeError(f"decision engine refused: {refusal}")
    return wire(engine(), get_provider())


async def close_engine() -> None:
    """Close the engine if one was built, and forget it: the next start
    builds its own."""
    if get_engine.cache_info().currsize == 0:
        return
    engine = get_engine()
    get_engine.cache_clear()
    close = getattr(engine, "aclose", None)
    if close is not None:
        await close()


def wire(engine, provider):
    """Hand the engine what it needs from the provider: how the platform
    writes an invoice reference, so a message can be masked (D10). An engine
    or a provider without the hook is left as it is."""
    finder = getattr(provider, "invoice_reference_spans", None)
    if finder is not None and hasattr(engine, "invoice_references"):
        engine.invoice_references = finder
    return engine


@lru_cache(maxsize=1)
def get_audit() -> SqliteAuditRepository:
    return SqliteAuditRepository(os.getenv("AUDIT_DB", "audit.db"))


@lru_cache(maxsize=1)
def get_attempts() -> SqliteAttemptStore:
    return SqliteAttemptStore(os.getenv("AUDIT_DB", "audit.db"))


@lru_cache(maxsize=1)
def get_actions() -> SqliteActionStore:
    return SqliteActionStore(os.getenv("AUDIT_DB", "audit.db"))


def get_approvals() -> ApprovalService:
    return ApprovalService(provider=get_provider(), actions=get_actions())


def get_investigator() -> DisputeInvestigator:
    return DisputeInvestigator(
        provider=get_provider(),
        engine=get_engine(),
        audit=get_audit(),
        actions=get_actions(),
    )

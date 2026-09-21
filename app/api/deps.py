"""Wiring. The only module that names a concrete provider or engine.

Everything else in ``app/`` depends on the two protocols. Swapping the billing
platform or the decision engine is an edit here and nowhere else -- which is
the claim of ADR-001 and ADR-002, made checkable.

Callers may ask for a particular engine or model per run, but only from an
allow-list this module owns. See :data:`ENGINES` for why that is not merely a
tidiness preference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from app.application.execute_action import ApprovalService
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import DecisionEngine
from app.decision.claude import PRICING_PER_MTOK, ClaudeDecisionEngine
from app.decision.mock import MockDecisionEngine
from app.infrastructure.actions import SqliteActionStore
from app.infrastructure.audit import SqliteAuditRepository
from app.providers.base import BillingProvider
from app.providers.fake.adapter import FakeBillingProvider


class UnknownEngine(ValueError):
    pass


class UnknownModel(ValueError):
    pass


@dataclass(frozen=True)
class EngineSpec:
    factory: type
    accepts_model: bool


#: Engines that exist today. The roadmap ones are absent on purpose: an empty
#: stub in this table would suggest an integration that is not there.
#:
#: This table is also a security boundary. A run may name the engine and model
#: it wants, but only from here -- never as a free string from the request.
#: On a system that proposes credit notes, letting the caller pick the judge
#: is privilege escalation wearing the clothes of a configuration option: the
#: obvious move is to ask for the weakest available model and see whether the
#: decision comes out differently.
#:
#: What the choice cannot do, by construction, is widen what the system is
#: allowed to execute. ``ActionPolicy`` never reads the model; it gates on the
#: *kind* of confidence and on the amount. Choosing a different engine changes
#: who answers, not what may be done with the answer.
ENGINES: dict[str, EngineSpec] = {
    "mock": EngineSpec(MockDecisionEngine, accepts_model=False),
    "claude": EngineSpec(ClaudeDecisionEngine, accepts_model=True),
}


def default_engine() -> str:
    return os.getenv("DECISION_ENGINE", "mock").lower()


def allowed_engines() -> list[str]:
    """Engines a caller may request, narrowed by ``ENGINE_ALLOWLIST``.

    In a real deployment this would not contain ``mock``.
    """
    configured = os.getenv("ENGINE_ALLOWLIST")
    if not configured:
        return sorted(ENGINES)
    return sorted({name.strip().lower() for name in configured.split(",")} & set(ENGINES))


def allowed_models() -> list[str]:
    """Models a caller may request, narrowed by ``MODEL_ALLOWLIST``.

    Defaults to the models we hold a price for: an unpriced model would record
    every run as costing nothing, which is worse than refusing it.
    """
    configured = os.getenv("MODEL_ALLOWLIST")
    if not configured:
        return sorted(PRICING_PER_MTOK)
    return sorted({m.strip() for m in configured.split(",")} & set(PRICING_PER_MTOK))


@lru_cache(maxsize=1)
def get_provider() -> BillingProvider:
    return FakeBillingProvider()


@lru_cache(maxsize=8)
def get_engine(name: str | None = None, model: str | None = None) -> DecisionEngine:
    """Build an engine, refusing anything outside the allow-lists.

    Refusing rather than falling back to a default: a silent substitution
    would make the audit row name an engine the caller did not get.
    """
    name = (name or default_engine()).lower()
    if name not in allowed_engines():
        raise UnknownEngine(f"unknown decision engine {name!r}; allowed: {allowed_engines()}")

    spec = ENGINES[name]
    if model is None:
        return spec.factory()

    if not spec.accepts_model:
        raise UnknownModel(f"engine {name!r} does not take a model")
    if model not in allowed_models():
        raise UnknownModel(f"model {model!r} is not allowed; allowed: {allowed_models()}")
    return spec.factory(model=model)


@lru_cache(maxsize=1)
def get_audit() -> SqliteAuditRepository:
    return SqliteAuditRepository(os.getenv("AUDIT_DB", "audit.db"))


@lru_cache(maxsize=1)
def get_actions() -> SqliteActionStore:
    return SqliteActionStore(os.getenv("AUDIT_DB", "audit.db"))


def get_approvals() -> ApprovalService:
    return ApprovalService(provider=get_provider(), actions=get_actions())


def build_investigator(
    engine: str | None = None, model: str | None = None
) -> DisputeInvestigator:
    return DisputeInvestigator(
        provider=get_provider(),
        engine=get_engine(engine, model),
        audit=get_audit(),
        actions=get_actions(),
    )


def get_investigator() -> DisputeInvestigator:
    """The default wiring, for callers that express no preference."""
    return build_investigator()

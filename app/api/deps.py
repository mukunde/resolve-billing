"""Wiring. The only module that names a concrete provider or engine.

Everything else in ``app/`` depends on the two protocols. Swapping the billing
platform or the decision engine is an edit here and nowhere else -- which is
the claim of ADR-001 and ADR-002, made checkable.
"""

from __future__ import annotations

import os
from functools import lru_cache

from app.application.execute_action import ApprovalService
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import DecisionEngine
from app.decision.claude import ClaudeDecisionEngine
from app.decision.mock import MockDecisionEngine
from app.infrastructure.actions import SqliteActionStore
from app.infrastructure.audit import SqliteAuditRepository
from app.providers.base import BillingProvider
from app.providers.fake.adapter import FakeBillingProvider

#: Engines that exist today. The roadmap ones are absent on purpose: an empty
#: stub in this table would suggest an integration that is not there.
ENGINES: dict[str, type] = {
    "mock": MockDecisionEngine,
    "claude": ClaudeDecisionEngine,
}


@lru_cache(maxsize=1)
def get_provider() -> BillingProvider:
    return FakeBillingProvider()


@lru_cache(maxsize=1)
def get_engine() -> DecisionEngine:
    name = os.getenv("DECISION_ENGINE", "mock").lower()
    engine = ENGINES.get(name)
    if engine is None:
        raise RuntimeError(
            f"unknown decision engine {name!r}; available: {sorted(ENGINES)}"
        )
    return engine()


@lru_cache(maxsize=1)
def get_audit() -> SqliteAuditRepository:
    return SqliteAuditRepository(os.getenv("AUDIT_DB", "audit.db"))


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

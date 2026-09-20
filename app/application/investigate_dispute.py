"""The vertical slice: a dispute in, an evidenced recommendation out.

Order matters and is the architecture in miniature:

1. load the billing context, once, in full;
2. run the deterministic rules -- they are free, instant and certain;
3. if the message tries to address the system, stop here and hand it to a
   human, without ever calling the model;
4. otherwise ask the decision engine to classify what is left;
5. compute the concrete action -- including its amount -- in code.

Step 5 is the one worth noticing. The engine says *propose a credit note*. It
never says *99,00 EUR*: the amount comes from the invoice, deterministically.
A probabilistic component is not allowed to pick a number that moves money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from app.application.events import AuditEvent, AuditSink, event
from app.decision.base import (
    Classification,
    Decision,
    DecisionEngine,
    EngineTelemetry,
    RecommendedAction,
)
from app.domain.enums import RuleCode
from app.domain.models import BillingContext, Dispute, RuleFinding
from app.domain.money import Money
from app.domain.rules import (
    detect_duplicate_invoice,
    invoice_after_cancellation,
    proration_mismatch,
    suspected_prompt_injection,
    suspicious_usage_delta,
    unknown_invoice,
)
from app.policies.action_policy import ActionAuthorization, ActionPolicy, Verdict
from app.providers.base import BillingProvider

#: How far back to pull invoices when assembling the context.
LOOKBACK = timedelta(days=180)


class ProposedAction(BaseModel):
    """What the system suggests doing, with an amount computed in code."""

    model_config = ConfigDict(frozen=True)

    type: Literal["credit_note", "refund", "reply", "request_information", "none"]
    amount: Money | None = None


class InvestigationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    dispute: Dispute
    classification: Classification
    confidence: float
    confidence_kind: str
    decision_engine: str
    decision_engine_version: str
    findings: list[RuleFinding]
    explanation: str
    proposed_action: ProposedAction
    authorization: ActionAuthorization
    events: list[AuditEvent] = []
    action_id: str | None = None
    """Set when a financial action is queued for a human to approve."""
    telemetry: EngineTelemetry | None = None
    short_circuited_by: RuleCode | None = None

    @property
    def requires_human_approval(self) -> bool:
        return self.authorization.requires_human_approval


class DisputeInvestigator:
    """Wires a billing provider to a decision engine. Knows neither by name."""

    def __init__(
        self,
        provider: BillingProvider,
        engine: DecisionEngine,
        policy: ActionPolicy | None = None,
        audit: AuditSink | None = None,
        actions=None,
    ) -> None:
        self._provider = provider
        self._engine = engine
        self._policy = policy or ActionPolicy()
        self._audit = audit
        self._actions = actions

    async def investigate(
        self, dispute: Dispute, now: datetime | None = None
    ) -> InvestigationResult:
        now = now or datetime.now(UTC)
        trace = [event("run_started", engine=self._engine.name)]

        context = await self._load_context(dispute, now)
        trace.append(
            event(
                "billing_context_loaded",
                invoices=len(context.invoices),
                subscriptions=len(context.subscriptions),
            )
        )

        findings = self._run_rules(dispute, context)
        trace.extend(
            event("rule_evaluated", code=f.code.value, severity=f.severity.value)
            for f in findings
        )

        injection = next(
            (f for f in findings if f.code is RuleCode.SUSPECTED_PROMPT_INJECTION), None
        )
        if injection is not None:
            trace.append(event("injection_suspected", matched=injection.evidence.get("categories")))
            trace.append(event("decision_engine_skipped"))
            return self._finish(
                self._short_circuit(dispute, context, findings, injection), trace
            )

        decision = await self._engine.decide(dispute, context, findings)
        trace.append(
            event(
                "decision_created",
                classification=decision.classification.value,
                confidence=decision.confidence,
                cost_micros=decision.telemetry.cost_micros if decision.telemetry else 0,
            )
        )
        return self._finish(self._assemble(dispute, context, findings, decision), trace)

    def _finish(
        self, result: InvestigationResult, trace: list[AuditEvent]
    ) -> InvestigationResult:
        verdict = result.authorization.verdict.value
        trace.append(event("authorization_evaluated", verdict=verdict))
        if result.requires_human_approval:
            trace.append(event("approval_requested", verdict=verdict))

        action_id = self._queue_for_approval(result, trace)
        recorded = result.model_copy(update={"events": trace, "action_id": action_id})
        if self._audit is not None:
            self._audit.record(recorded, billing_provider=self._provider.name)
        return recorded

    def _queue_for_approval(
        self, result: InvestigationResult, trace: list[AuditEvent]
    ) -> str | None:
        """Register the action a human will be asked about.

        Queued here rather than by the HTTP layer so the pending approval
        exists whoever triggered the run -- n8n, a test, or a future batch.
        """
        if self._actions is None:
            return None
        if result.authorization.verdict is not Verdict.REQUIRES_APPROVAL:
            return None
        amount = result.proposed_action.amount
        invoice_id = result.dispute.invoice_id
        if amount is None or invoice_id is None:
            return None

        action = self._actions.create(
            run_id=result.run_id,
            action_type=result.proposed_action.type,
            invoice_id=invoice_id,
            amount=amount,
            reason=result.explanation,
            provider=self._provider.name,
        )
        trace.append(event("action_queued", action_id=action.action_id, state=action.state.value))
        return action.action_id

    # -- steps -------------------------------------------------------------

    async def _load_context(self, dispute: Dispute, now: datetime) -> BillingContext:
        customer = None
        if dispute.customer_id:
            customer = await self._provider.get_customer(dispute.customer_id)
        elif dispute.customer_email:
            customer = await self._provider.find_customer_by_email(dispute.customer_email)
        if customer is None:
            raise ValueError("a dispute must identify a customer by id or by email")

        subscriptions = await self._provider.list_subscriptions(customer.id)
        invoices = await self._provider.list_invoices(customer.id, since=now - LOOKBACK)
        payments = await self._provider.list_payments(customer.id)

        target = next((i for i in invoices if i.id == dispute.invoice_id), None)
        usage = None
        if target is not None and target.subscription_id:
            usage = await self._provider.get_usage(target.subscription_id)

        return BillingContext(
            customer=customer,
            subscriptions=subscriptions,
            invoices=invoices,
            payments=payments,
            usage=usage,
            target_invoice_id=target.id if target else None,
        )

    def _run_rules(self, dispute: Dispute, context: BillingContext) -> list[RuleFinding]:
        findings: list[RuleFinding] = []

        # Screened first, on the raw text, before anything else touches it.
        injection = suspected_prompt_injection(dispute.customer_message)
        if injection is not None:
            findings.append(injection)

        if dispute.invoice_id:
            missing = unknown_invoice(dispute.invoice_id, context.invoices)
            if missing is not None:
                findings.append(missing)

        target = context.target_invoice
        if target is None:
            return findings

        for finding in (
            detect_duplicate_invoice(target, context.invoices),
            *self._subscription_rules(target, context),
            self._usage_rule(target, context),
        ):
            if finding is not None:
                findings.append(finding)
        return findings

    def _subscription_rules(self, target, context: BillingContext) -> tuple:
        sub = context.subscription(target.subscription_id)
        if sub is None:
            return ()
        return (
            invoice_after_cancellation(target, sub),
            proration_mismatch(target, sub),
        )

    def _usage_rule(self, target, context: BillingContext) -> RuleFinding | None:
        """Compare metered usage against the quantity actually billed for it."""
        usage = context.usage
        if usage is None:
            return None
        line = next((line for line in target.lines if line.metric == usage.metric), None)
        if line is None:
            return None
        return suspicious_usage_delta(expected=usage.quantity, billed=line.quantity)

    # -- assembly ----------------------------------------------------------

    def _short_circuit(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
        injection: RuleFinding,
    ) -> InvestigationResult:
        """The model is never asked about a message that targets the system."""
        return InvestigationResult(
            run_id=_new_run_id(),
            dispute=dispute,
            classification=Classification.UNKNOWN,
            confidence=1.0,
            confidence_kind="calibrated",  # a rule fired; there is nothing to estimate
            decision_engine="none",
            decision_engine_version="",
            findings=findings,
            explanation=deterministic_explanation(findings),
            proposed_action=ProposedAction(type="none"),
            authorization=ActionAuthorization(
                verdict=Verdict.HUMAN_REVIEW,
                reason=f"Constat bloquant : {injection.code.value}.",
            ),
            short_circuited_by=injection.code,
        )

    def _assemble(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
        decision: Decision,
    ) -> InvestigationResult:
        action = _proposed_action(decision, context)
        return InvestigationResult(
            run_id=_new_run_id(),
            dispute=dispute,
            classification=decision.classification,
            confidence=decision.confidence,
            confidence_kind=decision.confidence_kind,
            decision_engine=self._engine.name,
            decision_engine_version=self._engine.version,
            findings=findings,
            explanation=decision.explanation or deterministic_explanation(findings),
            proposed_action=action,
            authorization=self._policy.evaluate(decision, findings, action.amount),
            telemetry=decision.telemetry,
        )


def _proposed_action(decision: Decision, context: BillingContext) -> ProposedAction:
    """Turn a recommended *kind* of action into a concrete one.

    The amount is read off the disputed invoice. No model picks it.
    """
    match decision.recommended_action:
        case RecommendedAction.PROPOSE_CREDIT_NOTE | RecommendedAction.PROPOSE_REFUND:
            invoice = context.target_invoice
            if invoice is None:
                return ProposedAction(type="none")
            kind = (
                "credit_note"
                if decision.recommended_action is RecommendedAction.PROPOSE_CREDIT_NOTE
                else "refund"
            )
            return ProposedAction(type=kind, amount=invoice.total)
        case RecommendedAction.EXPLAIN_INVOICE:
            return ProposedAction(type="reply")
        case RecommendedAction.REQUEST_INFORMATION:
            return ProposedAction(type="request_information")
        case _:
            return ProposedAction(type="none")


def deterministic_explanation(findings: list[RuleFinding]) -> str:
    """Build an explanation out of the findings, with no model involved.

    Used whenever the engine returns none. Traceable line by line, which is
    what an audit row wants.
    """
    if not findings:
        return "Aucun contrôle déterministe n'a relevé d'anomalie sur cette facture."
    return "Constats déterministes : " + " ".join(
        f"[{finding.code.value}] {finding.description}" for finding in findings
    )


def _new_run_id() -> str:
    return f"run_{uuid4().hex[:8]}"

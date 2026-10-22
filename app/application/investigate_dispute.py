"""The vertical slice: a dispute in, an evidenced recommendation out.

Order matters and is the architecture in miniature:

1. load the billing context, once, in full -- or, when the sender matches
   no customer, stop there: there is no context to load, and nothing about
   any account is said to an address we do not know (ADR-007);
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

import logging
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
    EngineUnavailable,
    RecommendedAction,
)
from app.domain.automated import automated_message
from app.domain.enums import RuleCode, SenderStatus, Severity
from app.domain.models import (
    BillingContext,
    Customer,
    Dispute,
    Invoice,
    InvoiceOwner,
    RuleFinding,
)
from app.domain.money import Money
from app.domain.rules import (
    checks_not_evaluable,
    cited_invoice_conflict,
    currency_mismatch,
    detect_duplicate_invoice,
    duplicate_payment,
    identity_conflict,
    invoice_after_cancellation,
    proration_mismatch,
    suspected_impersonation,
    suspected_prompt_injection,
    suspicious_usage_delta,
    unknown_customer,
    unknown_invoice,
)
from app.observability import tracing
from app.policies.action_policy import ActionAuthorization, ActionPolicy, Verdict
from app.policies.correction import correction_for
from app.policies.reply import AutomaticReply, automatic_reply
from app.providers.base import BillingProvider, ProviderUnavailable, ProviderUnsupported

log = logging.getLogger(__name__)

#: Findings meaning the evidence cannot settle the dispute: a person looks,
#: and the engine is not asked to reason over what the rules could not use
#: (taxonomy S7, S10).
_TO_A_PERSON = frozenset({RuleCode.CURRENCY_MISMATCH, RuleCode.INVOICE_OUTSIDE_WINDOW})

#: How far back to pull invoices when assembling the context.
LOOKBACK = timedelta(days=180)

#: The longest message handed to a decision engine, in characters. Past it,
#: the run still happens -- the rules run on the billing context, the audit
#: records it -- but the engine is not asked, and a person reads the message.
#: A cost and context bound, not a judgement on the customer. D12.
ENGINE_MESSAGE_LIMIT = 10_000

#: The longest message the service accepts at all, to protect itself. Past it
#: the API refuses, and the orchestrator alerts a person. Quoted history and
#: signatures are the support tool's to strip upstream (ADR-010).
MESSAGE_HARD_LIMIT = 200_000


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
    recommended_action: RecommendedAction | None = None
    """What the engine asked for, before translation. None when it was not
    called. Kept because `proposed_action` alone cannot tell an escalation
    from a credit note that had no invoice to apply to: both become `none`."""
    authorization: ActionAuthorization
    events: list[AuditEvent] = []
    action_id: str | None = None
    """Set when a financial action is queued for a human to approve."""
    telemetry: EngineTelemetry | None = None
    short_circuited_by: RuleCode | None = None
    sender: SenderStatus = SenderStatus.KNOWN
    """Whether we know who wrote. Decided by the rules, never by the engine."""
    automatic_reply: AutomaticReply = AutomaticReply()
    """Whether text may be sent to the sender unattended, and why or why not.
    The only authority on an automatic reply (ADR-011); set once the run is
    finished, false until then."""

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
        """Investigate one dispute, and trace the run as a whole.

        The span wraps rather than threads through: the body below is exactly
        what it was before tracing existed, and with no exporter configured
        the span is a no-op object. Observability that changes the behaviour
        it observes is worse than none.
        """
        with tracing.span("invoke_workflow investigate_dispute") as current:
            try:
                result = await self._investigate(dispute, now)
            except ProviderUnavailable as error:
                # Nothing is decided on a partial read (taxonomy S7). Recorded
                # here and in the trace, by category only: the exception's text
                # may carry a URL or the platform's answer. The caller is told,
                # and can retry.
                log.warning(
                    "billing platform unavailable: dispute=%s error=%s",
                    dispute.id,
                    type(error).__name__,
                )
                tracing.set_attributes(current, **{"error.type": "provider_unavailable"})
                raise
            except ProviderUnsupported as error:
                # Data the domain cannot represent: nothing is decided, no run
                # is recorded, and the category -- never the data -- is said.
                log.warning(
                    "billing platform data not supported: dispute=%s reason=%s",
                    dispute.id,
                    error.reason,
                )
                tracing.set_attributes(current, **{"error.type": "provider_unsupported"})
                raise
            tracing.set_attributes(
                current,
                **tracing.workflow_attributes(
                    run_id=result.run_id,
                    provider=self._provider.name,
                    engine=result.decision_engine,
                ),
                **tracing.decision_attributes(
                    classification=result.classification.value,
                    confidence=result.confidence,
                    confidence_kind=result.confidence_kind,
                ),
                # Codes from a closed set, never the evidence behind them.
                **{
                    "billing.verdict": result.authorization.verdict.value,
                    "billing.requires_human_approval": result.requires_human_approval,
                    "billing.findings": ",".join(f.code.value for f in result.findings),
                },
            )
            return result

    async def _investigate(
        self, dispute: Dispute, now: datetime | None = None
    ) -> InvestigationResult:
        now = now or datetime.now(UTC)
        trace = [event("run_started", engine=self._engine.name)]

        customer = await self._find_customer(dispute)
        conflict = identity_conflict(dispute.customer_id, customer)
        if conflict is not None:
            return self._finish(self._identity_conflict(dispute, conflict, trace), trace)
        if customer is None:
            return self._finish(await self._unknown_sender(dispute, trace), trace)

        context = await self._load_context_for(customer, dispute, now)
        trace.append(
            event(
                "billing_context_loaded",
                invoices=len(context.invoices),
                subscriptions=len(context.subscriptions),
            )
        )

        findings = self.run_rules(dispute, context)
        trace.extend(
            event("rule_evaluated", code=f.code.value, severity=f.severity.value)
            for f in findings
        )
        # Under-billing is the business's loss, not the customer's dispute
        # (taxonomy S1): an audit note, never a finding put to the engine or
        # in a reply.
        under = [
            f
            for f in findings
            if f.code is RuleCode.SUSPICIOUS_USAGE_DELTA and f.evidence.get("direction") == "under"
        ]
        for finding in under:
            trace.append(
                event(
                    "usage_under_billed",
                    expected=finding.evidence["expected"],
                    billed=finding.evidence["billed"],
                )
            )
        findings = [f for f in findings if f not in under]

        injection = next(
            (f for f in findings if f.code is RuleCode.SUSPECTED_PROMPT_INJECTION), None
        )
        if injection is not None:
            trace.append(event("injection_suspected", matched=injection.evidence.get("categories")))
            trace.append(event("decision_engine_skipped"))
            return self._finish(self._short_circuit(dispute, findings, injection), trace)

        response = next((f for f in findings if f.code is RuleCode.AUTOMATED_RESPONSE), None)
        if response is not None:
            trace.append(event("decision_engine_skipped"))
            return self._finish(self._set_aside(dispute, findings, response), trace)

        too_long = _message_too_long(dispute.customer_message)
        if too_long is not None:
            findings.append(too_long)
            trace.append(event("message_too_long", **too_long.evidence))
            trace.append(event("decision_engine_skipped"))
            return self._finish(self._short_circuit(dispute, findings, too_long), trace)

        conflict = next(
            (f for f in findings if f.code is RuleCode.INVOICE_REFERENCE_CONFLICT), None
        )
        if conflict is not None:
            trace.append(event("decision_engine_skipped"))
            return self._finish(self._ask_which_invoice(dispute, findings, conflict), trace)

        unusable = next((f for f in findings if f.code in _TO_A_PERSON), None)
        if unusable is not None:
            trace.append(event("decision_engine_skipped"))
            result = self._short_circuit(
                dispute,
                findings,
                unusable,
                reason=f"Revue humaine : {unusable.code.value}. {unusable.description}",
            )
            return self._finish(result, trace)

        try:
            decision = await self._engine.decide(dispute, context, findings)
        except EngineUnavailable as outage:
            # An outage is not an abstention (taxonomy S8): the trace says the
            # engine could not be reached, no decision event is written for
            # an answer that never came, and no cost is invented.
            trace.append(event("decision_engine_unavailable", reason=str(outage)))
            decision = _escalation_after_outage(str(outage))
            return self._finish(self._assemble(dispute, context, findings, decision), trace)
        trace.append(
            event(
                "decision_created",
                classification=decision.classification.value,
                confidence=decision.confidence,
                cost_micros=decision.telemetry.cost_micros if decision.telemetry else None,
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
        reply = automatic_reply(result)
        trace.append(event("automatic_reply", allowed=reply.allowed, reason=reply.reason.value))
        recorded = result.model_copy(
            update={"events": trace, "action_id": action_id, "automatic_reply": reply}
        )
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

    async def load_context(self, dispute: Dispute, now: datetime) -> BillingContext:
        customer = await self._find_customer(dispute)
        if customer is None or identity_conflict(dispute.customer_id, customer):
            raise ValueError("a dispute must name the address of a known customer")
        return await self._load_context_for(customer, dispute, now)

    async def _find_customer(self, dispute: Dispute) -> Customer | None:
        """Who wrote, by the sender's address and the provider's own lookup (D9).

        A `customer_id` on the dispute is a claim, checked against this by
        `identity_conflict`; it never decides who the sender is.
        """
        if dispute.customer_email:
            return await self._provider.find_customer_by_email(dispute.customer_email)
        return None

    async def _load_context_for(
        self, customer: Customer, dispute: Dispute, now: datetime
    ) -> BillingContext:
        subscriptions = await self._provider.list_subscriptions(customer.id)
        invoices = await self._provider.list_invoices(customer.id, since=now - LOOKBACK)
        payments = await self._provider.list_payments(customer.id)

        target = next((i for i in invoices if i.id == dispute.invoice_id), None)
        # Not found in the window is not "does not exist" (taxonomy S7): ask
        # whose the reference is, and nothing else -- nothing about another
        # account's invoice is read, so nothing of it can surface.
        outside_window = False
        if dispute.invoice_id and target is None:
            owner = await self._provider.invoice_owner(dispute.invoice_id)
            outside_window = owner == customer.id
        usage = None
        period = _metered_period(target) if target is not None else None
        if period is not None and target.subscription_id:
            usage = await self._provider.get_usage(target.subscription_id, period)

        return BillingContext(
            customer=customer,
            subscriptions=subscriptions,
            invoices=invoices,
            payments=payments,
            usage=usage,
            target_invoice_id=target.id if target else None,
            target_outside_window=outside_window,
        )

    def run_rules(self, dispute: Dispute, context: BillingContext) -> list[RuleFinding]:
        """Every deterministic check, in the order that matters.

        Public because it has a second consumer: the MCP server exposes the
        findings without a decision. Duplicating the wiring there would risk
        the two drifting on the one thing that must not drift -- the injection
        screen runs first, on the raw text, before anything else touches it.
        """
        findings: list[RuleFinding] = []

        # Screened first, on the raw text, before anything else touches it.
        injection = suspected_prompt_injection(dispute.customer_message)
        if injection is not None:
            findings.append(injection)

        automated = automated_message(dispute.customer_email, dispute.headers)
        if automated is not None:
            findings.append(automated)

        references = getattr(self._provider, "invoice_references", None)
        conflict = (
            cited_invoice_conflict(
                references(dispute.customer_message),
                next(iter(references(dispute.invoice_id)), dispute.invoice_id)
                if dispute.invoice_id
                else None,
            )
            if references is not None
            else None
        )
        if conflict is not None:
            findings.append(conflict)

        if dispute.invoice_id:
            missing = unknown_invoice(dispute.invoice_id, context.invoices)
            if missing is not None and context.target_outside_window:
                missing = _outside_window(dispute.invoice_id)
            if missing is not None:
                findings.append(missing)

        target = context.target_invoice
        if target is None:
            return findings

        for finding in (
            currency_mismatch(target, context.invoices, context.payments),
            detect_duplicate_invoice(target, context.invoices),
            duplicate_payment(target, context.payments),
            *self._subscription_rules(target, context),
            *self._usage_rules(target, context),
            checks_not_evaluable(
                target, context.subscription(target.subscription_id), context.invoices
            ),
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

    def _usage_rules(self, target, context: BillingContext) -> tuple:
        """Compare metered usage against the quantity billed for it, line by
        line; a metered line with no usage record to compare it with is said,
        so that silence is never read as "the usage is right" (taxonomy S7)."""
        usage = context.usage
        metered = [line for line in target.lines if line.metric]
        delta = None
        unchecked: list[str] = []
        for line in metered:
            # The same subscription, the same metric, and a record for exactly
            # the line's own, real period: a September reading says nothing
            # about an August line, and a monthly one nothing about a week of
            # it until a rule for splitting usage exists.
            comparable = (
                usage is not None
                and usage.subscription_id == target.subscription_id
                and line.metric == usage.metric
                and line.period_seconds is not None
                and line.period_seconds > 0
                and usage.period_start == line.period_start
                and usage.period_end == line.period_end
            )
            # One usage record against one line. A metric billed on several
            # lines would need a rule for adding them up, which nothing
            # specifies yet: those lines are said unchecked, not guessed at.
            alone = sum(other.metric == line.metric for other in metered) == 1
            if comparable and alone:
                delta = suspicious_usage_delta(expected=usage.quantity, billed=line.quantity)
            else:
                unchecked.append(line.metric)
        return delta, _usage_unavailable(target.id, unchecked) if unchecked else None

    async def _unknown_sender(
        self, dispute: Dispute, trace: list[AuditEvent]
    ) -> InvestigationResult:
        """The sender matches no customer. See ADR-007.

        No billing context exists to give the engine, so it is never called.
        The injection screen still runs first, on the raw text. Then either
        the message reaches for a known customer's account -- a human looks,
        and nothing is proposed -- or it does not, and the sender is asked to
        write from the address we know.
        """
        trace.append(event("customer_not_found"))

        findings: list[RuleFinding] = []
        injection = suspected_prompt_injection(dispute.customer_message)
        if injection is not None:
            findings.append(injection)

        unknown = unknown_customer(dispute.customer_email, None)
        assert unknown is not None  # no customer was found: the rule always fires
        findings.append(unknown)

        automated = automated_message(dispute.customer_email, dispute.headers)
        if automated is not None:
            findings.append(automated)

        # Whose the cited invoice is, and nothing else of it: the sender is
        # nobody we know.
        owner = (
            await self._provider.invoice_owner(dispute.invoice_id) if dispute.invoice_id else None
        )
        impersonation = suspected_impersonation(
            dispute.customer_email,
            dispute.customer_message,
            InvoiceOwner(dispute.invoice_id, owner) if owner is not None else None,
            await self._provider.list_customers(),
        )
        if impersonation is not None:
            findings.append(impersonation)

        trace.extend(
            event("rule_evaluated", code=f.code.value, severity=f.severity.value)
            for f in findings
        )
        if injection is not None:
            trace.append(event("injection_suspected", matched=injection.evidence.get("categories")))
        trace.append(event("decision_engine_skipped"))

        sender = (
            SenderStatus.SUSPECTED_IMPERSONATION
            if impersonation is not None
            else SenderStatus.UNKNOWN
        )
        # ADR-011's order: an injection, an automated response, a suspected
        # impersonation, an automated message, then a person. Impersonation
        # outranks bulk mail: a marketing mail naming a customer costs one
        # review, a fraud dressed as marketing costs more.
        if injection is not None:
            return self._short_circuit(dispute, findings, injection, sender)
        if automated is not None and automated.code is RuleCode.AUTOMATED_RESPONSE:
            return self._set_aside(dispute, findings, automated, sender)
        if impersonation is not None:
            return self._short_circuit(dispute, findings, impersonation, sender)
        if automated is not None:
            return self._set_aside(dispute, findings, automated, sender)
        return self._ask_for_known_address(dispute, findings, unknown)

    # -- assembly ----------------------------------------------------------

    def _short_circuit(
        self,
        dispute: Dispute,
        findings: list[RuleFinding],
        blocking: RuleFinding,
        sender: SenderStatus = SenderStatus.KNOWN,
        reason: str | None = None,
    ) -> InvestigationResult:
        """The model is never asked about a message a blocking rule caught:
        one that targets the system, or one that reaches for an account its
        sender does not hold."""
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
                reason=reason or f"Constat bloquant : {blocking.code.value}.",
            ),
            short_circuited_by=blocking.code,
            sender=sender,
        )

    def _set_aside(
        self,
        dispute: Dispute,
        findings: list[RuleFinding],
        automated: RuleFinding,
        sender: SenderStatus = SenderStatus.KNOWN,
    ) -> InvestigationResult:
        """Automated mail with nothing to act on (ADR-011): an auto-reply or a
        delivery report, or bulk mail from someone nobody can be asked.

        Nothing is proposed and nobody is alerted -- an approver buried under
        out-of-office replies stops reading -- but the run is recorded like
        any other. The verdict is set here rather than by the policy, as for
        every short circuit: there is no decision for the policy to read.
        """
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
                verdict=Verdict.AUTO,
                reason=(
                    f"Message automatique ({automated.code.value}) : rien à proposer, "
                    "personne à alerter, aucune réponse automatique."
                ),
            ),
            short_circuited_by=automated.code,
            sender=sender,
        )

    def _identity_conflict(
        self, dispute: Dispute, conflict: RuleFinding, trace: list[AuditEvent]
    ) -> InvestigationResult:
        """The request names an account its sender's address does not resolve
        to (D9). Nothing is loaded for the claimed account, the engine is not
        called, nothing is proposed or said: a person checks."""
        findings: list[RuleFinding] = []
        injection = suspected_prompt_injection(dispute.customer_message)
        if injection is not None:
            findings.append(injection)
        findings.append(conflict)
        trace.extend(
            event("rule_evaluated", code=f.code.value, severity=f.severity.value)
            for f in findings
        )
        if injection is not None:
            trace.append(event("injection_suspected", matched=injection.evidence.get("categories")))
        trace.append(event("decision_engine_skipped"))
        return self._short_circuit(
            dispute,
            findings,
            injection or conflict,
            SenderStatus.SUSPECTED_IMPERSONATION,
        )

    def _ask_which_invoice(
        self,
        dispute: Dispute,
        findings: list[RuleFinding],
        conflict: RuleFinding,
    ) -> InvestigationResult:
        """The message names other invoices than the one designated (taxonomy
        S6): ask which, rather than investigate -- or correct -- the wrong one.
        A question moves no money, so the policy authorises it as usual."""
        decision = Decision(
            classification=Classification.INSUFFICIENT_INFORMATION,
            confidence=1.0,
            confidence_kind="calibrated",  # a rule fired; there is nothing to estimate
            recommended_action=RecommendedAction.REQUEST_INFORMATION,
        )
        return InvestigationResult(
            run_id=_new_run_id(),
            dispute=dispute,
            classification=decision.classification,
            confidence=decision.confidence,
            confidence_kind=decision.confidence_kind,
            decision_engine="none",
            decision_engine_version="",
            findings=findings,
            explanation=deterministic_explanation(findings),
            proposed_action=ProposedAction(type="request_information"),
            recommended_action=decision.recommended_action,
            authorization=self._policy.evaluate(decision, findings),
            short_circuited_by=conflict.code,
        )

    def _ask_for_known_address(
        self,
        dispute: Dispute,
        findings: list[RuleFinding],
        unknown: RuleFinding,
    ) -> InvestigationResult:
        """Propose asking the sender to write from the address we know.

        Authorised by the policy like any engine decision would be: a question
        moves no money. But it is never sent automatically (ADR-011): nobody
        can tell this sender from a forged address, so a person sends it, or
        not. The explanation is built from the findings alone, so the
        request names no customer.
        """
        decision = Decision(
            classification=Classification.INSUFFICIENT_INFORMATION,
            confidence=1.0,
            confidence_kind="calibrated",  # a rule fired; there is nothing to estimate
            recommended_action=RecommendedAction.REQUEST_INFORMATION,
        )
        return InvestigationResult(
            run_id=_new_run_id(),
            dispute=dispute,
            classification=decision.classification,
            confidence=decision.confidence,
            confidence_kind=decision.confidence_kind,
            decision_engine="none",
            decision_engine_version="",
            findings=findings,
            explanation=deterministic_explanation(findings),
            proposed_action=ProposedAction(type="request_information"),
            authorization=self._policy.evaluate(decision, findings),
            short_circuited_by=unknown.code,
            sender=SenderStatus.UNKNOWN,
        )

    def _assemble(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
        decision: Decision,
    ) -> InvestigationResult:
        action, withheld = _proposed_action(decision, context, findings)
        authorization = self._policy.evaluate(decision, findings, action.amount, withheld)
        if action.type in {"credit_note", "refund"} and authorization.verdict not in {
            Verdict.REQUIRES_APPROVAL,
            Verdict.AUTO,
        }:
            # Refused money is not shown as a proposal (taxonomy S12): what
            # the result offers matches what the policy allows. What the engine
            # asked for stays in `recommended_action`, the refusal in the reason.
            action = ProposedAction(type="none")
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
            recommended_action=decision.recommended_action,
            authorization=authorization,
            telemetry=decision.telemetry,
        )


def _proposed_action(
    decision: Decision, context: BillingContext, findings: list[RuleFinding]
) -> tuple[ProposedAction, str | None]:
    """Turn a recommended *kind* of action into a concrete one.

    The amount is computed in code from the records, never picked by a model,
    and only when the code can stand behind it (`correction_for`). The second
    element says why a financial action got no amount.
    """
    match decision.recommended_action:
        case RecommendedAction.PROPOSE_CREDIT_NOTE | RecommendedAction.PROPOSE_REFUND:
            invoice = context.target_invoice
            if invoice is None:
                return ProposedAction(type="none"), None
            kind = (
                "credit_note"
                if decision.recommended_action is RecommendedAction.PROPOSE_CREDIT_NOTE
                else "refund"
            )
            correction = correction_for(kind, invoice, findings, context.payments)
            return ProposedAction(type=kind, amount=correction.amount), correction.withheld
        case RecommendedAction.EXPLAIN_INVOICE:
            return ProposedAction(type="reply"), None
        case RecommendedAction.REQUEST_INFORMATION:
            return ProposedAction(type="request_information"), None
        case _:
            return ProposedAction(type="none"), None


def _outside_window(invoice_id: str) -> RuleFinding:
    """The customer's invoice, older than the loaded window (taxonomy S7)."""
    return RuleFinding(
        code=RuleCode.INVOICE_OUTSIDE_WINDOW,
        severity=Severity.MEDIUM,
        description=(
            f"La facture existe mais sort de la fenêtre d'analyse ({LOOKBACK.days} jours) : "
            "rien n'a été vérifié, une personne examine."
        ),
        evidence={"invoice_id": invoice_id, "window_days": LOOKBACK.days},
    )


def _metered_period(invoice: Invoice) -> tuple[datetime, datetime] | None:
    """The one real period every metered line of `invoice` covers, or None.

    Usage is read for it (FR-016 as amended, option (a)). Lines on distinct
    periods, or on no real one, ask for nothing: no line is picked, and the
    usage rules say them unchecked. An invoice without metered lines asks for
    nothing either -- no rule has a line to compare a record with.
    """
    metered = [line for line in invoice.lines if line.metric]
    periods = {(line.period_start, line.period_end) for line in metered}
    if len(periods) != 1 or not (metered[0].period_seconds or 0) > 0:
        return None
    return periods.pop()


def _usage_unavailable(invoice_id: str, metrics: list[str]) -> RuleFinding:
    """Metered lines with no usage record to compare them with (taxonomy S7)."""
    return RuleFinding(
        code=RuleCode.USAGE_UNAVAILABLE,
        severity=Severity.LOW,
        description="Usage mesuré indisponible : des lignes à l'usage n'ont pas pu être vérifiées.",
        evidence={"invoice_id": invoice_id, "metrics": sorted(set(metrics))},
    )


def _escalation_after_outage(reason: str) -> Decision:
    """What an unreachable engine yields: a person, and no cost invented."""
    return Decision(
        classification=Classification.UNKNOWN,
        confidence=0.0,
        confidence_kind="self_reported",
        recommended_action=RecommendedAction.ESCALATE,
        explanation=(
            f"Moteur de décision indisponible ({reason}) : le litige part en revue humaine."
        ),
    )


def _message_too_long(message: str) -> RuleFinding | None:
    """A finding when the message is longer than an engine is given. D12."""
    if len(message) <= ENGINE_MESSAGE_LIMIT:
        return None
    return RuleFinding(
        code=RuleCode.MESSAGE_TOO_LONG,
        severity=Severity.MEDIUM,
        description=(
            f"Message de {len(message)} caractères, au-delà des {ENGINE_MESSAGE_LIMIT} "
            "soumis au moteur : à lire par une personne."
        ),
        evidence={"length": len(message), "limit": ENGINE_MESSAGE_LIMIT},
    )


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

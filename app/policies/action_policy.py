"""Who is allowed to execute what. See ADR-004.

The decision engine *recommends*. This module *authorises*. Keeping those two
apart is the whole safety argument: swapping the engine, or having it go
wrong, cannot widen what the system is permitted to do.

The policy reads the decision and the findings. It never reads the customer's
message -- nothing a customer writes should be able to influence what the
system is allowed to do to their account.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.decision.base import FINANCIAL_ACTIONS, Classification, Decision, RecommendedAction
from app.domain.enums import RuleCode, Severity
from app.domain.models import RuleFinding
from app.domain.money import Money


class Verdict(StrEnum):
    AUTO = "auto"
    """Execute without a human. Read-only work, and nothing else today."""

    REQUIRES_APPROVAL = "requires_approval"
    """A concrete action is on the table; a human says yes or no."""

    HUMAN_REVIEW = "human_review"
    """No action is safe to propose. A human decides what happens next."""


class ActionAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    reason: str
    autonomy_available: bool = False
    """True when an autonomy threshold *could* have applied but was withheld.

    Recorded so the audit shows the difference between "no rule would have
    allowed this" and "a rule would have, and we chose not to trust it yet".
    """

    @property
    def requires_human_approval(self) -> bool:
        return self.verdict is not Verdict.AUTO


class AutonomyPolicy(BaseModel):
    """Thresholds above which a financial action could run unattended.

    Present in the code, configurable, and **off**. The reason is written into
    the type it depends on: a model's confidence is ``self_reported`` until an
    evaluation run measures it, and gating money on an unmeasured number is
    gating it on nothing.

    ``required_confidence_kind`` is what actually holds the gate shut. Even
    with ``enabled=True``, a self-reported 0.99 does not clear a policy that
    demands a calibrated one.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    min_confidence: float = 0.95
    max_amount_minor: int = 5_000
    required_confidence_kind: str = "calibrated"


#: Findings that stop the system acting, whatever the engine concluded.
BLOCKING_FINDINGS = frozenset(
    {RuleCode.SUSPECTED_PROMPT_INJECTION, RuleCode.SUSPECTED_IMPERSONATION}
)

#: Classifications on which no financial action may be taken, whatever the
#: engine recommends. Proposing a credit note while unable to say what
#: happened is incoherent, not merely risky.
NO_MONEY_ON = frozenset({Classification.UNKNOWN, Classification.INSUFFICIENT_INFORMATION})

#: Classifications where the system has nothing safe to say either.
#:
#: ``INSUFFICIENT_INFORMATION`` is deliberately *not* here, which is a
#: departure from a literal reading of the plan ("insufficient evidence or
#: unknown -> human review"). Asking the customer which invoice they mean is
#: not an action taken on their account: it is the safe non-action, and the
#: draft is reviewed before it is sent like every other reply. Routing it to a
#: human would put a person in the loop of "we need more information", which
#: buys nothing and trains reviewers to click through.
#:
#: What the plan is protecting against is *acting* on thin evidence, and
#: ``NO_MONEY_ON`` above is what protects it.
NOTHING_SAFE_TO_SAY = frozenset({Classification.UNKNOWN})


class ActionPolicy:
    """Turns a recommendation into an authorization."""

    def __init__(self, autonomy: AutonomyPolicy | None = None) -> None:
        self.autonomy = autonomy or AutonomyPolicy()

    def evaluate(
        self,
        decision: Decision,
        findings: list[RuleFinding],
        amount: Money | None = None,
        withheld: str | None = None,
    ) -> ActionAuthorization:
        """`withheld` says why no amount could be computed, when none was."""
        # 1. A blocking finding outranks everything, including a confident
        #    engine and a real duplicate sitting right there in the evidence.
        blocking = next((f for f in findings if f.code in BLOCKING_FINDINGS), None)
        if blocking is not None:
            return ActionAuthorization(
                verdict=Verdict.HUMAN_REVIEW,
                reason=f"Constat bloquant : {blocking.code.value}.",
            )

        # 2. Money, including the case where money was recommended on a
        #    classification that cannot support it.
        if decision.recommended_action in FINANCIAL_ACTIONS:
            if decision.classification in NO_MONEY_ON:
                return ActionAuthorization(
                    verdict=Verdict.HUMAN_REVIEW,
                    reason=(
                        "Action financière recommandée alors que le litige n'est pas "
                        f"qualifié ({decision.classification.value})."
                    ),
                )
            return self._financial(decision, amount, withheld)

        # 3. An engine that asks for a human gets one, whatever it classified.
        #    Read off the action rather than inferred from the classification:
        #    the schema accepts any pairing, and a run of prompt v2 paired
        #    `escalate` with `duplicate_billing` -- which this policy then
        #    authorised as an automatic reply. A prompt rule fixed the
        #    symptom; it is the policy that has to hold whatever the engine.
        if decision.recommended_action is RecommendedAction.ESCALATE:
            return ActionAuthorization(
                verdict=Verdict.HUMAN_REVIEW,
                reason=(
                    "Le moteur demande une revue humaine "
                    f"(classification : {decision.classification.value})."
                ),
            )

        # 4. Not knowing what happened leaves nothing safe to say either.
        if decision.classification in NOTHING_SAFE_TO_SAY:
            return ActionAuthorization(
                verdict=Verdict.HUMAN_REVIEW,
                reason=(
                    "Le moteur n'a pas su classer le litige de façon exploitable "
                    f"({decision.classification.value})."
                ),
            )

        # 5. Everything else is a reply or a question: no money moves, and the
        #    draft is reviewed before it is sent anyway.
        return ActionAuthorization(
            verdict=Verdict.AUTO,
            reason="Action non financière, sans effet sur le compte du client.",
        )

    def _financial(
        self, decision: Decision, amount: Money | None, withheld: str | None = None
    ) -> ActionAuthorization:
        if amount is None:
            reason = "Action financière recommandée sans montant calculable"
            return ActionAuthorization(
                verdict=Verdict.HUMAN_REVIEW,
                reason=f"{reason} : {withheld}." if withheld else f"{reason}.",
            )

        would_qualify = (
            decision.confidence >= self.autonomy.min_confidence
            and amount.amount_minor <= self.autonomy.max_amount_minor
        )
        trusted = decision.confidence_kind == self.autonomy.required_confidence_kind

        if self.autonomy.enabled and would_qualify and trusted:
            return ActionAuthorization(
                verdict=Verdict.AUTO,
                reason=(
                    f"Sous le seuil d'autonomie ({amount}), "
                    f"confiance {decision.confidence_kind} de {decision.confidence:.2f}."
                ),
                autonomy_available=True,
            )

        return ActionAuthorization(
            verdict=Verdict.REQUIRES_APPROVAL,
            reason=_why_not_autonomous(self.autonomy, decision, would_qualify),
            autonomy_available=would_qualify,
        )


def _why_not_autonomous(
    autonomy: AutonomyPolicy, decision: Decision, would_qualify: bool
) -> str:
    if not autonomy.enabled:
        return (
            "Mutation financière : validation humaine obligatoire. Les seuils "
            "d'autonomie sont désactivés tant que la confiance du moteur n'est "
            "pas calibrée sur le jeu d'évaluation."
        )
    if decision.confidence_kind != autonomy.required_confidence_kind:
        return (
            f"Confiance de type « {decision.confidence_kind} », alors que la "
            f"politique exige « {autonomy.required_confidence_kind} »."
        )
    if not would_qualify:
        return "Montant ou confiance hors des seuils d'autonomie."
    return "Mutation financière : validation humaine obligatoire."


def blocking_severity(findings: list[RuleFinding]) -> Severity | None:
    """Highest severity among the findings, for the audit row."""
    order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
    if not findings:
        return None
    return max((f.severity for f in findings), key=lambda s: order[s])

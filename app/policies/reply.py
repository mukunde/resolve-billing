"""May an automatic reply be sent? See ADR-011.

A second question beside the action policy's. The verdict says whether an
action on the account needs a person; this says whether sending text to this
address, unattended, is safe. They were one flag until D11, and a reply that
needs no approval is not thereby a reply anyone may receive.

Computed by code, after the policy, from the result alone -- never by the
engine. False unless every condition holds.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.domain.enums import RuleCode, SenderStatus
from app.policies.action_policy import Verdict

if TYPE_CHECKING:
    from app.application.investigate_dispute import InvestigationResult

#: Findings meaning the message came from software.
AUTOMATED = frozenset({RuleCode.AUTOMATED_RESPONSE, RuleCode.AUTOMATED_MESSAGE})

#: Actions that are text sent to the sender.
REPLIES = frozenset({"reply", "request_information"})


class ReplyReason(StrEnum):
    """Why an automatic reply is allowed or not. A closed set, for routing."""

    ALLOWED = "allowed"
    AUTOMATED_MESSAGE = "automated_message"
    UNKNOWN_SENDER = "unknown_sender"
    SUSPECTED_IMPERSONATION = "suspected_impersonation"
    NEEDS_A_PERSON = "needs_a_person"
    NOTHING_TO_SEND = "nothing_to_send"
    NOT_EVALUATED = "not_evaluated"


class AutomaticReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool = False
    reason: ReplyReason = ReplyReason.NOT_EVALUATED


def automatic_reply(result: InvestigationResult) -> AutomaticReply:
    """The first condition that fails names the reason; none failing allows."""
    if any(f.code in AUTOMATED for f in result.findings):
        return AutomaticReply(reason=ReplyReason.AUTOMATED_MESSAGE)
    if result.sender is SenderStatus.SUSPECTED_IMPERSONATION:
        return AutomaticReply(reason=ReplyReason.SUSPECTED_IMPERSONATION)
    if result.sender is not SenderStatus.KNOWN:
        return AutomaticReply(reason=ReplyReason.UNKNOWN_SENDER)
    if result.authorization.verdict is not Verdict.AUTO:
        return AutomaticReply(reason=ReplyReason.NEEDS_A_PERSON)
    if result.proposed_action.type not in REPLIES:
        return AutomaticReply(reason=ReplyReason.NOTHING_TO_SEND)
    return AutomaticReply(allowed=True, reason=ReplyReason.ALLOWED)

"""Closed vocabularies of the billing domain.

Every one of these is a closed set on purpose. A value the system does not know
must fail loudly at the provider boundary rather than travel through the rules
as a bare string.
"""

from enum import StrEnum


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class ActionState(StrEnum):
    r"""Lifecycle of a proposed financial action.

    PostgreSQL (SQLite here) holds this, not the orchestrator. If the state of
    truth lived in n8n, a restart would lose every pending approval, silently
    and with no trace. n8n is an approval interface and a trigger; it is
    replaceable.

        PENDING_APPROVAL --approve--> APPROVED --execute--> EXECUTED
               |                                        \-> FAILED
               |--reject---> REJECTED
               \--ttl-----> EXPIRED
    """

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"


class Severity(StrEnum):
    """How much a finding should weigh on the action policy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SenderStatus(StrEnum):
    """Who wrote, as far as the rules can tell. See ADR-007.

    A separate axis from the classification on purpose: the classification
    says what a dispute is about, this says whether we know who is asking.
    Set by the rules only. The decision engine never sees it and cannot
    return it.
    """

    KNOWN = "known"
    UNKNOWN = "unknown"
    SUSPECTED_IMPERSONATION = "suspected_impersonation"


class RuleCode(StrEnum):
    """Codes emitted by the deterministic rules.

    Closed set: a finding code is a contract between the rules, the action
    policy, the audit log and the evaluation suite.
    """

    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    INVOICE_AFTER_CANCELLATION = "INVOICE_AFTER_CANCELLATION"
    SUSPICIOUS_USAGE_DELTA = "SUSPICIOUS_USAGE_DELTA"
    PRORATION_MISMATCH = "PRORATION_MISMATCH"
    UNKNOWN_INVOICE = "UNKNOWN_INVOICE"
    SUSPECTED_PROMPT_INJECTION = "SUSPECTED_PROMPT_INJECTION"
    UNKNOWN_CUSTOMER = "UNKNOWN_CUSTOMER"
    SUSPECTED_IMPERSONATION = "SUSPECTED_IMPERSONATION"

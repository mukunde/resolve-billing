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

        PENDING_APPROVAL --approve--> APPROVED --claim--> EXECUTING --> EXECUTED
               |                                   ^                |-> FAILED
               |--reject---> REJECTED              |                \-> UNKNOWN_OUTCOME
               \--ttl-----> EXPIRED                \-- retry -- FAILED      |
                                                                 reconcile:
                                                     found -> EXECUTED, absent -> FAILED

    EXECUTING is the claim on the write: exactly one caller moves an action
    into it, and only that caller rings the provider. FAILED means the
    platform said no; UNKNOWN_OUTCOME means it did not answer, and is
    resolved by asking it, never by writing again blind. See ADR-008.
    """

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"
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
    # Not a billing rule: an input check, made before the engine. The message
    # is longer than what is sent to a model, so a person reads it instead.
    MESSAGE_TOO_LONG = "MESSAGE_TOO_LONG"
    # Input checks on automated mail (ADR-011): never an automatic reply.
    # An auto-reply or a delivery report, with nothing to investigate...
    AUTOMATED_RESPONSE = "AUTOMATED_RESPONSE"
    # ...and generated, bulk or list mail, investigated from a known sender.
    AUTOMATED_MESSAGE = "AUTOMATED_MESSAGE"
    # Evidence the rules cannot use (taxonomy S7, S10): said, never guessed.
    # Two amounts that must be compared are in different currencies...
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    # ...the disputed invoice is the customer's, older than what is loaded...
    INVOICE_OUTSIDE_WINDOW = "INVOICE_OUTSIDE_WINDOW"
    # ...or a metered line has no usage record to compare it with.
    USAGE_UNAVAILABLE = "USAGE_UNAVAILABLE"
    # One invoice collected twice: two distinct settled payments, each its
    # full total (the payment-side face of a duplicate).
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    # The message cites invoice references, none of them the designated one
    # (taxonomy S6): ask which, rather than investigate the wrong invoice.
    INVOICE_REFERENCE_CONFLICT = "INVOICE_REFERENCE_CONFLICT"
    # The request names a customer the sender's address does not resolve to
    # (D9): a claim, not an identity. Blocking.
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"

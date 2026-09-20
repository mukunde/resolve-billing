"""Pseudonymise the billing context before it reaches a decision engine.

The threat model says the engine never sees an internal identifier. This is
the part that makes that true, and it buys two distinct things:

**The engine cannot name another customer's invoice.** It only ever sees
aliases we minted for this run, so a reference outside that set is not a
mistake to be corrected -- it is a value that cannot be mapped back, and is
dropped.

**The blast radius of a successful injection shrinks.** Persuading the model
to cite ``INVOICE_9`` achieves nothing if no such alias was issued.

The mapping lives for one run and is thrown away with it.
"""

from __future__ import annotations

from app.domain.models import BillingContext, RuleFinding


class Pseudonyms:
    """A one-run, two-way map between real identifiers and opaque aliases."""

    def __init__(self) -> None:
        self._to_alias: dict[str, str] = {}
        self._to_real: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def alias(self, real_id: str, kind: str) -> str:
        existing = self._to_alias.get(real_id)
        if existing is not None:
            return existing
        self._counters[kind] = self._counters.get(kind, 0) + 1
        alias = f"{kind}_{self._counters[kind]}"
        self._to_alias[real_id] = alias
        self._to_real[alias] = real_id
        return alias

    def resolve(self, alias: str) -> str | None:
        """Real identifier behind an alias, or None if we never issued it."""
        return self._to_real.get(alias)

    def resolve_all(self, aliases: list[str]) -> list[str]:
        """Map back the aliases we issued, silently dropping the rest.

        Dropping rather than raising is deliberate: a hallucinated or coaxed
        reference is not an exceptional condition, it is the expected failure
        mode, and the run should continue with one less citation.
        """
        resolved = [self.resolve(alias) for alias in aliases]
        return [real for real in resolved if real is not None]

    def known_aliases(self) -> set[str]:
        return set(self._to_real)


def anonymise(context: BillingContext, findings: list[RuleFinding]) -> tuple[dict, Pseudonyms]:
    """Render the context and findings with aliases only.

    Returns the payload the engine may see, and the map needed to read its
    answer. Amounts, dates, statuses and quantities pass through unchanged --
    they are what the decision is about.
    """
    names = Pseudonyms()
    names.alias(context.customer.id, "CUSTOMER")

    for subscription in context.subscriptions:
        names.alias(subscription.id, "SUBSCRIPTION")
    for invoice in context.invoices:
        names.alias(invoice.id, "INVOICE")
    for payment in context.payments:
        names.alias(payment.id, "PAYMENT")

    payload = {
        "customer": {
            "ref": names.alias(context.customer.id, "CUSTOMER"),
            "currency": context.customer.currency,
        },
        "disputed_invoice_ref": (
            names.alias(context.target_invoice_id, "INVOICE")
            if context.target_invoice_id
            else None
        ),
        "subscriptions": [
            {
                "ref": names.alias(s.id, "SUBSCRIPTION"),
                "status": s.status.value,
                "plan_code": s.plan_code,
                "period_start": s.current_period_start.isoformat(),
                "period_end": s.current_period_end.isoformat(),
                "cancelled_at": s.cancelled_at.isoformat() if s.cancelled_at else None,
            }
            for s in context.subscriptions
        ],
        "invoices": [
            {
                "ref": names.alias(i.id, "INVOICE"),
                "subscription_ref": (
                    names.alias(i.subscription_id, "SUBSCRIPTION") if i.subscription_id else None
                ),
                "status": i.status.value,
                "issued_at": i.issued_at.isoformat(),
                "total_minor": i.total.amount_minor,
                "currency": i.total.currency,
                "lines": [
                    {
                        "description": line.description,
                        "quantity": str(line.quantity),
                        "amount_minor": line.amount.amount_minor,
                        "metric": line.metric,
                        "period_start": (
                            line.period_start.isoformat() if line.period_start else None
                        ),
                        "period_end": line.period_end.isoformat() if line.period_end else None,
                    }
                    for line in i.lines
                ],
            }
            for i in context.invoices
        ],
        "payments": [
            {
                "ref": names.alias(p.id, "PAYMENT"),
                "invoice_ref": names.alias(p.invoice_id, "INVOICE") if p.invoice_id else None,
                "status": p.status.value,
                "amount_minor": p.amount.amount_minor,
                "currency": p.amount.currency,
                "received_at": p.received_at.isoformat() if p.received_at else None,
            }
            for p in context.payments
        ],
        "metered_usage": (
            {
                "subscription_ref": names.alias(context.usage.subscription_id, "SUBSCRIPTION"),
                "metric": context.usage.metric,
                "quantity": str(context.usage.quantity),
            }
            if context.usage
            else None
        ),
        "deterministic_findings": [
            {
                "code": finding.code.value,
                "severity": finding.severity.value,
                "description": finding.description,
                "evidence": _alias_evidence(finding.evidence, names),
            }
            for finding in findings
        ],
    }
    return payload, names


def _alias_evidence(evidence: dict, names: Pseudonyms) -> dict:
    """Replace any identifier inside a finding's evidence with its alias.

    Findings carry raw ids in their evidence, which is right for the audit log
    and wrong for the model. Values we never issued an alias for are left
    alone -- they are amounts, dates and counts, not references.
    """
    aliased: dict = {}
    for key, value in evidence.items():
        if isinstance(value, str):
            aliased[key] = names._to_alias.get(value, value)
        elif isinstance(value, list):
            aliased[key] = [
                names._to_alias.get(item, item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            aliased[key] = value
    return aliased

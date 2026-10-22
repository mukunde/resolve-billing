"""A read-only MCP server over the billing port.

Tools for somebody else's agent, rather than tools inside ours.

The decision engine in this project holds no tools: the billing context is
loaded eagerly, pseudonymised and handed over complete, because a model that
calls a tool mid-reasoning decides its own reach at inference time, and here
that reach is customer billing data (ADR-006).

That argument is about *our* agent. It says nothing against letting another
agent -- a client's copilot, an internal assistant, an evaluation harness --
read what this system can read. So the same `BillingProvider` port is exposed
over MCP, with one property enforced rather than promised:

**Nothing here can move money.** The provider's mutating methods -- a credit
note and a refund -- are not reachable from any tool below, the approval
state machine is not exposed, and every tool is annotated
`read_only_hint=True` so a client can see the guarantee in the protocol
rather than in this docstring.

What an external agent gets is the *facts and the deterministic findings* --
never a recommendation. `run_deterministic_checks` returns what code
established; it does not call a model, costs nothing, and cannot propose a
credit note. Deciding what to do about a finding stays behind the action
policy, where the audit row and the human approval are.

    python -m app.mcp.server            # stdio, for a local MCP client
"""

from __future__ import annotations

import functools
import os
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.application.investigate_dispute import DisputeInvestigator
from app.domain.models import Dispute
from app.providers.base import BillingProvider, ProviderUnsupported

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

server = MCPServer(
    name="billing-investigation",
    instructions=(
        "Read-only access to a billing platform and to the deterministic "
        "checks this system runs over it. Nothing here mutates anything: no "
        "credit note, no refund, no approval. Use run_deterministic_checks to "
        "find out what the rules establish about an invoice before forming an "
        "opinion of your own -- their silence is an answer, not an omission."
    ),
)


def _said(tool):
    """Data the platform holds but the domain cannot represent is an
    anticipated failure: the client reads its category, never the data
    (specs/001-stripe-read-adapter, FR-002). Any other error stays a crash,
    whose text the SDK withholds."""

    @functools.wraps(tool)
    async def run(*args, **kwargs):
        try:
            return await tool(*args, **kwargs)
        except ProviderUnsupported as error:
            raise ToolError(f"platform data not supported: {error.reason}") from None

    return run


def _provider() -> BillingProvider:
    from app.api.deps import get_provider

    return get_provider()


def _investigator() -> DisputeInvestigator:
    """An investigator wired for reads only.

    The engine is never reached: no tool below calls `investigate`. It is
    constructed because `load_context` and `run_rules` live on this class, and
    duplicating their wiring here would risk the two drifting on the one thing
    that must not -- the order the checks run in.
    """
    from app.api.deps import ENGINES

    return DisputeInvestigator(provider=_provider(), engine=ENGINES["mock"]())


@server.tool(
    description=(
        "Find a customer by e-mail. Returns the customer and the identifiers "
        "needed by the other tools, or null when no such customer exists."
    ),
    annotations=READ_ONLY,
)
@_said
async def find_customer(email: str) -> dict[str, Any] | None:
    customer = await _provider().find_customer_by_email(email)
    return customer.model_dump(mode="json") if customer else None


@server.tool(
    description="Fetch one invoice, with its lines, by identifier.",
    annotations=READ_ONLY,
)
@_said
async def get_invoice(invoice_id: str) -> dict[str, Any]:
    invoice = await _provider().get_invoice(invoice_id)
    return invoice.model_dump(mode="json")


@server.tool(
    description=(
        "The whole billing situation around a dispute: the customer, their "
        "subscriptions, recent invoices, payments and metered usage. Loaded "
        "in one call on purpose -- an agent that fetches these piecemeal ends "
        "up reasoning over a moving target."
    ),
    annotations=READ_ONLY,
)
@_said
async def get_billing_context(customer_email: str, invoice_id: str | None = None) -> dict[str, Any]:
    dispute = Dispute(
        id="mcp_read",
        customer_message="",
        customer_email=customer_email,
        invoice_id=invoice_id,
    )
    context = await _investigator().load_context(dispute, datetime.now(UTC))
    return context.model_dump(mode="json")


@server.tool(
    description=(
        "Run the deterministic billing checks and return what they found: "
        "duplicate invoices, billing after cancellation, proration mismatches, "
        "usage deltas, unknown invoice references, and prompt-injection "
        "screening of the customer's own words. No model is called, so this "
        "costs nothing and returns the same answer every time. An empty list "
        "means the checks found nothing, which is a result and not a gap."
    ),
    annotations=READ_ONLY,
)
@_said
async def run_deterministic_checks(
    customer_email: str,
    customer_message: str = "",
    invoice_id: str | None = None,
) -> dict[str, Any]:
    investigator = _investigator()
    dispute = Dispute(
        id="mcp_checks",
        customer_message=customer_message,
        customer_email=customer_email,
        invoice_id=invoice_id,
    )
    context = await investigator.load_context(dispute, datetime.now(UTC))
    findings = investigator.run_rules(dispute, context)
    return {
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "engine_called": False,
        "cost_micros": 0,
    }


def main() -> None:
    server.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()

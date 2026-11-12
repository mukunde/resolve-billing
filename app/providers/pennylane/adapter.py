"""Pennylane behind the billing port (specs/014-pennylane-read), writing credit
notes on the declared sandbox (specs/015-pennylane-credit-notes).

Every read starts with the company check (the declared sandbox only, Q2) and
runs within one time budget; every list is read to its end; refusals come from
the mapper with their reason. Writes happen only when built with a
`PennylaneWriter` (`PENNYLANE_WRITES=enabled`, a token of its own): then one
type, the credit note, in several requests and with no idempotency key --
`idempotency_window` is None. Without a writer `writes` is empty and the writes
refuse before any call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.enums import SubscriptionStatus
from app.domain.models import Customer, Invoice, Payment, Subscription, TaxBreakdown, Usage
from app.providers.base import (
    ALLOCATIONS,
    Allocation,
    BeforeRequest,
    NotFound,
    PartialWrite,
    PlatformCannotWrite,
    ProviderAction,
    ProviderOutcomeUnknown,
    ProviderRejected,
    ProviderUnavailable,
    ProviderUnsupported,
)
from app.providers.pennylane import mapping
from app.providers.pennylane.client import (
    DEFAULT_BUDGET,
    REQUEST_TIMEOUT,
    PennylaneReader,
    PennylaneWriter,
    Refused,
    Uncertain,
)
from app.providers.pennylane.keys import ProviderConfigurationError

Payload = dict[str, Any]

#: Seconds one write may take, every request included: under the step deadline
#: of two minutes the execution service's hook enforces (FR-011).
WRITE_BUDGET = 100.0
#: Pennylane's words when a create reuses an `external_reference` (422), seen
#: on the sandbox on 2026-11-12.
REFERENCE_TAKEN = "External reference has already been taken"
#: The day a credit note is dated: the company's own (Paris), so that it is
#: never dated before the latest invoice it follows.
COMPANY_ZONE = "Europe/Paris"


def _filter(field: str, value: Any) -> str:
    return json.dumps([{"field": field, "operator": "eq", "value": value}])


class PennylaneBillingProvider:
    name = "pennylane"
    can_write = False
    writes: frozenset[str] = frozenset()
    #: Pennylane keeps no idempotency key: the first send happens, a creation is
    #: never resent, an absence is never proof (spec 015, FR-007).
    idempotency_window = None
    #: Counted by the execution service's step deadline (FR-011).
    request_timeout = REQUEST_TIMEOUT

    def __init__(
        self,
        reader: PennylaneReader,
        budget: float = DEFAULT_BUDGET,
        *,
        writer: PennylaneWriter | None = None,
        write_budget: float = WRITE_BUDGET,
        now: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.reader = reader
        self._budget = budget
        self._writer = writer
        self.can_write = writer is not None
        self.writes = frozenset({"credit_note"}) if writer is not None else frozenset()
        self._write_budget = write_budget
        self._now = now or (lambda: datetime.now(UTC))
        self._today = today or self._company_day

    def _company_day(self) -> date:
        try:
            return self._now().astimezone(ZoneInfo(COMPANY_ZONE)).date()
        except ZoneInfoNotFoundError:
            return self._now().date()

    # -- references (FR-010) -------------------------------------------------

    def invoice_references(self, text: str, invoices: Sequence[Invoice] = ()) -> set[str]:
        return {invoice_id for _, _, invoice_id in mapping.references(text, invoices)}

    def invoice_reference_spans(
        self, text: str, invoices: Sequence[Invoice] = ()
    ) -> list[tuple[int, int, str]]:
        return mapping.references(text, invoices)

    def resolve_invoice_reference(self, reference: str, invoices: Sequence[Invoice]) -> str:
        """The disputed invoice (Q3): a typed id as it is; else the one loaded
        invoice whose whole number it is; else the reference as given."""
        if mapping.raw_id(reference, "invoice") is not None:
            return reference
        return mapping.resolve_number(reference, invoices) or reference

    def is_invoice_id(self, reference: str) -> bool:
        return mapping.raw_id(reference, "invoice") is not None

    # -- customers -------------------------------------------------------------

    async def get_customer(self, customer_id: str) -> Customer:
        async with self._read():
            raw = mapping.raw_id(customer_id, "customer")
            if raw is None:
                raise NotFound(f"unknown customer {customer_id!r}")
            return mapping.customer(await self.reader.get(f"/customers/{raw}"))

    async def find_customer_by_email(self, email: str) -> Customer | None:
        """The first customer, in id order, any of whose addresses is the one
        given, compared trimmed and case-folded; mapped, so a customer with two
        addresses is refused (`multiple_addresses`)."""
        wanted = email.strip().lower()
        async with self._read():
            for payload in await self._customers():
                if wanted in {address.lower() for address in mapping.emails(payload)}:
                    return mapping.customer(payload)
        return None

    async def list_customers(self) -> list[Customer]:
        async with self._read():
            return [mapping.customer(payload) for payload in await self._customers()]

    async def _customers(self) -> list[Payload]:
        found = await self.reader.every("/customers", {"sort": "id"})
        return sorted(found, key=lambda payload: _int(payload.get("id")))

    # -- subscriptions ---------------------------------------------------------------

    async def get_subscription(self, subscription_id: str) -> Subscription:
        async with self._read():
            raw = mapping.raw_id(subscription_id, "subscription")
            if raw is None:
                raise NotFound(f"unknown subscription {subscription_id!r}")
            return mapping.subscription(await self.reader.get(f"/billing_subscriptions/{raw}"))

    async def list_subscriptions(self, customer_id: str) -> list[Subscription]:
        async with self._read():
            return await self._subscriptions(customer_id)

    async def _subscriptions(self, customer_id: str) -> list[Subscription]:
        raw = mapping.raw_id(customer_id, "customer")
        if raw is None:
            return []
        found = await self.reader.every(
            "/billing_subscriptions", {"filter": _filter("customer_id", raw)}
        )
        return [mapping.subscription(payload) for payload in found]

    # -- invoices --------------------------------------------------------------------

    async def get_invoice(self, invoice_id: str) -> Invoice:
        found = await self.find_invoice(invoice_id)
        if found is None:
            raise NotFound(f"unknown invoice {invoice_id!r}")
        return found

    async def find_invoice(self, invoice_id: str) -> Invoice | None:
        async with self._read():
            raw = mapping.raw_id(invoice_id, "invoice")
            if raw is None:
                return None
            try:
                payload = await self.reader.get(f"/customer_invoices/{raw}")
            except NotFound:
                return None
            customer_raw = mapping.raw_id(mapping.owner(payload), "customer")
            documents = await self._documents(customer_raw)
            subscriptions = await self._subscriptions_of(payload, customer_raw)
            return await self._invoice(payload, mapping.credited_ids(
                d for d in documents if mapping.is_credit_note(d)), subscriptions)

    async def invoice_owner(self, invoice_id: str) -> str | None:
        """Whose an invoice is: the invoice alone is read, nothing else."""
        async with self._read():
            raw = mapping.raw_id(invoice_id, "invoice")
            if raw is None:
                return None
            try:
                payload = await self.reader.get(f"/customer_invoices/{raw}")
            except NotFound:
                return None
            return mapping.owner(payload)

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]:
        """The customer's invoices issued at or after `since`; a refused one in
        that window refuses the call, an older one is not mapped. The customer's
        credit notes are all read, whatever their date."""
        async with self._read():
            raw = mapping.raw_id(customer_id, "customer")
            if raw is None:
                return []
            documents = await self._documents(raw)
            credited = mapping.credited_ids(d for d in documents if mapping.is_credit_note(d))
            recent = [
                d for d in documents
                if not mapping.is_credit_note(d)
                and d.get("status") not in mapping.NOT_INVOICES
                and mapping.midnight(mapping.issue_day(d)) >= since
            ]
            subscriptions = (
                {s.id: s for s in await self._subscriptions(customer_id)}
                if any(d.get("billing_subscription") for d in recent)
                else {}
            )
            return [await self._invoice(d, credited, subscriptions) for d in recent]

    async def _documents(
        self, customer_raw: int | None, before: BeforeRequest | None = None
    ) -> list[Payload]:
        """Every invoice and credit note of the customer, imported ones
        included, to the end."""
        if customer_raw is None:
            return []
        return await self.reader.every(
            "/customer_invoices",
            {
                "filter": _filter("customer_id", customer_raw),
                "include_invoices_created_from_ledger_entries": "true",
            },
            before=before,
        )

    async def _subscriptions_of(
        self, payload: Payload, customer_raw: int | None
    ) -> dict[str, Subscription]:
        if not payload.get("billing_subscription") or customer_raw is None:
            return {}
        return {s.id: s for s in await self._subscriptions(mapping.typed("customer", customer_raw))}

    async def _invoice(
        self, payload: Payload, credited: set[str], subscriptions: dict[str, Subscription]
    ) -> Invoice:
        identifier = mapping.typed("invoice", payload.get("id"))
        if mapping.is_credit_note(payload) or payload.get("status") in mapping.NOT_INVOICES:
            raise ProviderUnsupported("invoice_status")
        if identifier in credited:
            raise ProviderUnsupported("invoice_credit")
        lines = await self.reader.every(
            f"/customer_invoices/{mapping.raw_id(identifier, 'invoice')}/invoice_lines"
        )
        invoice = mapping.invoice(payload, lines)
        subscription = subscriptions.get(invoice.subscription_id or "")
        if (
            subscription is not None
            and subscription.status is SubscriptionStatus.CANCELLED
            and not mapping.fully_dated(invoice)
        ):
            # No check decides on partial coverage, nor on the issue instant.
            raise ProviderUnsupported("missing_field")
        return invoice

    # -- payments and usage ----------------------------------------------------------

    async def list_payments(self, customer_id: str) -> list[Payment]:
        """The payments of every invoice of the customer, every list to its end;
        a payment seen under two invoices is refused."""
        async with self._read():
            raw = mapping.raw_id(customer_id, "customer")
            if raw is None:
                return []
            payments: list[Payment] = []
            seen: dict[str, str] = {}
            for document in await self._documents(raw):
                if mapping.is_credit_note(document):
                    continue
                if document.get("status") in mapping.NOT_INVOICES:
                    continue
                invoice_id = mapping.typed("invoice", document.get("id"))
                found = await self.reader.every(
                    f"/customer_invoices/{mapping.raw_id(invoice_id, 'invoice')}/payments"
                )
                for item in found:
                    paid = mapping.payment(item, invoice_id)
                    if seen.setdefault(paid.id, invoice_id) != invoice_id:
                        raise ProviderUnsupported("multi_invoice_payment")
                    payments.append(paid)
            return payments

    async def get_usage(
        self, subscription_id: str, period: tuple[datetime, datetime]
    ) -> Usage | None:
        async with self._read():
            return None  # Pennylane holds no metered usage

    # -- writes: credit notes, on the declared sandbox (specs/015-pennylane-credit-notes) --

    def _writing(self, action_type: str) -> PennylaneWriter:
        """The writer, or a refusal before any call: writes off, or a type
        Pennylane does not write (a refund: the API has none, FR-001)."""
        if self._writer is None or action_type not in self.writes:
            raise PlatformCannotWrite(f"pennylane: does not write {action_type!r}")
        return self._writer

    def _lose_writes(self) -> None:
        """The write token's company is not the declared sandbox: from now
        on the platform writes nothing (FR-002)."""
        self.writes = frozenset()
        self.can_write = False

    async def _check_writer(self, writer: PennylaneWriter, before: BeforeRequest | None) -> None:
        try:
            await writer.check_company(before)
        except ProviderConfigurationError:
            self._lose_writes()
            raise ProviderRejected("sandbox") from None

    async def plan_write(self, invoice_id: str, action_type: str) -> Allocation:
        """Open (`upcoming`, `late`): what is due is lowered; paid: an amount
        owed to the customer; any other state takes no write (FR-003)."""
        writer = self._writing(action_type)
        async with self._read():
            await self._check_writer(writer, None)
            raw = mapping.raw_id(invoice_id, "invoice")
            if raw is None:
                raise ProviderRejected("not_found")
            try:
                payload = await self.reader.get(f"/customer_invoices/{raw}")
            except NotFound:
                raise ProviderRejected("not_found") from None
        return _allocation_for(payload)

    async def create_credit_note(
        self,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        idempotency_key: str,
        *,
        action_id: str,
        allocation: Allocation,
        send_by: datetime | None = None,
        breakdown: TaxBreakdown | None = None,
        before_request: BeforeRequest | None = None,
    ) -> ProviderAction:
        """One credit note, in FR-004's order: the checks, a draft carrying the
        key, read back and compared exactly with the approved figures, linked,
        checked again, finalised, read back. `before_request` runs before every
        request. A draft whose figures differ, or whose link Pennylane refuses
        for its cap, is deleted and seen gone before the refusal (FR-005);
        anything else uncertain after the creation is `PartialWrite` (FR-006).
        The `reason` never reaches Pennylane, nor any email or e-invoicing
        endpoint (FR-012)."""
        writer = self._writing("credit_note")
        if allocation not in ALLOCATIONS["credit_note"]:
            raise ProviderRejected("allocation")
        reference = mapping.reference(idempotency_key)
        if (
            mapping.OUR_REFERENCE.fullmatch(reference) is None
            or len(reference) > mapping.REFERENCE_MAX
        ):
            # Refused before anything is read or sent (FR-013).
            raise ProviderRejected("reference")
        invoice_raw = mapping.raw_id(invoice_id, "invoice")
        if invoice_raw is None:
            raise ProviderRejected("not_found")
        attempt = _Attempt(before_request)
        try:
            async with asyncio.timeout(self._write_budget):
                return await self._credit(
                    attempt, writer, invoice_raw, amount_minor, currency, reference,
                    allocation, breakdown,
                )
        except TimeoutError:
            raise attempt.unknown("time_budget") from None

    async def _credit(
        self,
        attempt: _Attempt,
        writer: PennylaneWriter,
        invoice_raw: int,
        amount_minor: int,
        currency: str,
        reference: str,
        allocation: Allocation,
        breakdown: TaxBreakdown | None,
    ) -> ProviderAction:
        hook = attempt.before
        # -- before anything is sent: reads and checks --------------------------
        await self._check_writer(writer, hook)
        try:
            await self.reader.check_company(hook)
            try:
                payload = await self.reader.get(f"/customer_invoices/{invoice_raw}", before=hook)
            except NotFound:
                raise ProviderRejected("not_found") from None
            if _allocation_for(payload) != allocation:
                raise ProviderRejected("invoice_state")
            lines = await self.reader.every(
                f"/customer_invoices/{invoice_raw}/invoice_lines", before=hook)
            customer_raw = mapping.raw_id(mapping.owner(payload), "customer")
            if customer_raw is None:
                raise ProviderUnsupported("missing_field")
            customer = await self.reader.get(f"/customers/{customer_raw}", before=hook)
            documents = await self._documents(customer_raw, hook)
            addresses = mapping.emails(customer)
        except (ProviderUnavailable, ProviderConfigurationError, ProviderUnsupported, NotFound):
            # After the claim, a read that fails leaves the outcome as unknown
            # as any other failure, as on Stripe: a person looks.
            raise ProviderOutcomeUnknown("invoice_read") from None
        if any(d.get("external_reference") == reference for d in documents):
            # Something carries this action's key already: never a second one.
            raise ProviderOutcomeUnknown("reference_taken")
        if any(mapping.is_ours(d) and _stage_or_unreadable(d) is not None for d in documents):
            # A credit note of ours left half-way for this customer: nothing
            # more is written until a person resolves it (FR-004).
            raise ProviderRejected("partial_document")
        if not mapping.outward_ready(customer, addresses):
            raise ProviderRejected("outward")
        if payload.get("currency") != currency:
            raise ProviderRejected("invoice_state")
        credit = mapping.credit_lines(payload, lines, amount_minor, breakdown)

        # -- the creation: sent once, never again -------------------------------
        today = self._today().isoformat()
        try:
            made = await writer.write("POST", "/customer_invoices", {
                "customer_id": customer_raw,
                "date": today,
                "deadline": today,
                "currency": currency,
                "draft": True,
                "external_reference": reference,
                "invoice_lines": credit,
            }, before=hook)
        except Uncertain:
            raise ProviderOutcomeUnknown("creation") from None
        except Refused as refusal:
            if refusal.status == 422 and REFERENCE_TAKEN in refusal.detail:
                raise ProviderOutcomeUnknown("reference_taken") from None
            raise ProviderRejected(f"refused ({refusal.status})") from None
        if not isinstance(made, dict):
            # Made, perhaps, with an answer that names nothing: a person looks.
            raise ProviderOutcomeUnknown("unreadable_answer")
        try:
            document_raw = _positive_int(made.get("id"))
        except ProviderUnsupported:
            raise ProviderOutcomeUnknown("unreadable_answer") from None
        attempt.document = mapping.typed("invoice", document_raw)
        attempt.stage = "draft"

        # -- after the creation: anything uncertain is a partial write -------------
        try:
            return await self._complete(
                attempt, writer, invoice_raw, customer_raw, document_raw, amount_minor,
                currency, reference, breakdown,
            )
        except (_NothingWritten, PartialWrite):
            raise
        except Exception:  # noqa: BLE001 -- every other failure leaves a document half-way
            raise PartialWrite(attempt.document, attempt.stage) from None

    async def _complete(
        self,
        attempt: _Attempt,
        writer: PennylaneWriter,
        invoice_raw: int,
        customer_raw: int,
        document_raw: int,
        amount_minor: int,
        currency: str,
        reference: str,
        breakdown: TaxBreakdown | None,
    ) -> ProviderAction:
        hook = attempt.before
        path = f"/customer_invoices/{document_raw}"
        draft = await self.reader.get(path, before=hook)
        draft_lines = await self.reader.every(f"{path}/invoice_lines", before=hook)
        if (
            not mapping.is_draft(draft)
            or mapping.credited_raw(draft) is not None
            or draft.get("external_reference") != reference
            or mapping.raw_id(mapping.owner(draft), "customer") != customer_raw
            or draft.get("currency") != currency
        ):
            raise PartialWrite(attempt.document, "draft")
        observed = mapping.observed_breakdown(draft, draft_lines)
        total = mapping.credit_total(draft)
        if observed != breakdown or total != amount_minor:
            await self._delete(attempt, writer, document_raw, customer_raw, reference)
            raise _NothingWritten(
                "draft_mismatch: Pennylane's draft "
                f"{_figures(observed, total, currency)}, approved "
                f"{_figures(breakdown, amount_minor, currency)}")

        attempt.stage = "linking"
        try:
            await writer.write(
                "POST", f"/customer_invoices/{invoice_raw}/link_credit_note",
                {"credit_note_id": document_raw}, before=hook)
        except Refused as refusal:
            if not mapping.is_cap_refusal(refusal):
                raise PartialWrite(attempt.document, "linking") from None
            # The cap: a draft confirmed still a draft, unlinked, is deleted.
            again = await self.reader.get(path, before=hook)
            if not mapping.is_draft(again) or mapping.credited_raw(again) is not None:
                raise PartialWrite(attempt.document, "linking") from None
            await self._delete(attempt, writer, document_raw, customer_raw, reference)
            raise _NothingWritten(f"cap: {mapping.CAP_MESSAGE}") from None

        attempt.stage = "linked"
        customer = await self.reader.get(f"/customers/{customer_raw}", before=hook)
        linked = await self.reader.get(path, before=hook)
        if (
            mapping.credited_raw(linked) != invoice_raw
            or not mapping.is_draft(linked)
            or not mapping.outward_ready(customer, mapping.emails(customer), linked)
        ):
            raise PartialWrite(attempt.document, "linked")

        attempt.stage = "finalising"
        await writer.write("PUT", f"{path}/finalize", before=hook)

        attempt.stage = "finalised"
        final = await self.reader.get(path, before=hook)
        final_lines = await self.reader.every(f"{path}/invoice_lines", before=hook)
        credited = await self.reader.get(f"/customer_invoices/{invoice_raw}", before=hook)
        made = mapping.provider_action(
            final, final_lines, credited, observed_at=self._now(), test_mode=True)
        if made.partial is not None:
            raise PartialWrite(attempt.document, "finalising")
        return made

    async def _delete(
        self,
        attempt: _Attempt,
        writer: PennylaneWriter,
        document_raw: int,
        customer_raw: int,
        reference: str,
    ) -> None:
        """Delete a draft, and see it gone -- by itself and from the customer's
        documents -- or it is a partial write (FR-005)."""
        attempt.stage = "deleting"
        path = f"/customer_invoices/{document_raw}"
        try:
            await writer.write("DELETE", path, before=attempt.before)
        except (Refused, Uncertain):
            raise PartialWrite(attempt.document, "deleting") from None
        try:
            await self.reader.get(path, before=attempt.before)
        except NotFound:
            pass
        else:
            raise PartialWrite(attempt.document, "deleting")
        documents = await self._documents(customer_raw, attempt.before)
        if any(
            d.get("id") == document_raw or d.get("external_reference") == reference
            for d in documents
        ):
            raise PartialWrite(attempt.document, "deleting")

    async def create_refund(self, *args, **kwargs) -> ProviderAction:
        """Pennylane's API makes no refund: a person carries it out (FR-001)."""
        raise PlatformCannotWrite("pennylane: does not write 'refund'")

    async def find_action(self, idempotency_key: str, invoice_id: str) -> list[ProviderAction]:
        """Every document of the invoice's customer carrying the key's
        reference, each read with its lines: complete, partial or unlike, the
        execution service tells which (FR-008). A read: it needs no writer, so
        a person's status read works with writes off. A document under the key
        that cannot be read makes the read fail, never an absence."""
        reference = mapping.reference(idempotency_key)
        async with self._read():
            raw = mapping.raw_id(invoice_id, "invoice")
            if raw is None:
                return []
            try:
                payload = await self.reader.get(f"/customer_invoices/{raw}")
            except NotFound:
                return []
            customer_raw = mapping.raw_id(mapping.owner(payload), "customer")
            found = [
                d for d in await self._documents(customer_raw)
                if d.get("external_reference") == reference
            ]
            observed = self._now()
            made = []
            for document in found:
                try:
                    document_raw = _positive_int(document.get("id"))
                    lines = await self.reader.every(
                        f"/customer_invoices/{document_raw}/invoice_lines")
                    # Read through the company check: the declared sandbox,
                    # and nothing else, is a test company (FR-013).
                    made.append(mapping.provider_action(
                        document, lines, payload, observed_at=observed, test_mode=True))
                except (ProviderUnsupported, NotFound):
                    raise ProviderUnavailable(
                        "pennylane: a document under the key cannot be read") from None
            return made

    # -- the budget and the company check ----------------------------------------------

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self._budget):
                await self.reader.check_company()
                yield
        except TimeoutError:
            raise ProviderUnavailable("pennylane: the read's time budget was spent") from None


def _int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderUnsupported("missing_field")
    return value


def _positive_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProviderUnsupported("missing_field")
    return value


#: Invoice statuses a credit note is written on, and how it lands (FR-003).
_ALLOCATION_OF_INVOICE: dict[str, Allocation] = {
    "upcoming": "reduce_due",
    "late": "reduce_due",
    "paid": "customer_balance",
}


def _allocation_for(payload: Payload) -> Allocation:
    if not mapping.is_invoice(payload):
        raise ProviderRejected("invoice_state")
    allocation = _ALLOCATION_OF_INVOICE.get(payload.get("status"))
    if allocation is None:
        raise ProviderRejected("invoice_state")
    return allocation


def _stage_or_unreadable(document: Payload) -> str | None:
    """What a document of ours lacks; "unreadable" when that cannot be told."""
    try:
        return mapping.partial_stage(document)
    except ProviderUnsupported:
        return "unreadable"


def _figures(breakdown: TaxBreakdown | None, total_minor: int, currency: str) -> str:
    """Figures for a failure's reason: minor units and rates, nothing else."""
    if breakdown is None:
        return f"total {total_minor} {currency}, no tax"
    taxes = ", ".join(
        f"{format(rate, 'f')}% {amount.amount_minor}" for rate, amount in breakdown.taxes
    )
    return (
        f"excluding tax {breakdown.excluding_tax.amount_minor}, tax {taxes}, "
        f"total {total_minor} {currency}"
    )


class _NothingWritten(ProviderRejected):
    """A draft deleted and seen gone: the two cases that end as nothing
    written (FR-005)."""


class _Attempt:
    """One execution's write: the document made, if any, and its stage."""

    def __init__(self, before_request: BeforeRequest | None) -> None:
        self._hook = before_request
        self.document: str | None = None
        self.stage = "none"

    async def before(self) -> None:
        """The execution service's check, before every request (FR-011)."""
        if self._hook is not None:
            await self._hook()

    def unknown(self, category: str) -> ProviderOutcomeUnknown:
        if self.document is not None:
            return PartialWrite(self.document, self.stage)
        return ProviderOutcomeUnknown(category)

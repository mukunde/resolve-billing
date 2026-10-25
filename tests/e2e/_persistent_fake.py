"""A fake billing platform that outlives the service, for A4
(specs/006-restart-failures). A test double: it lives in `tests/`, never in
the product.

`PersistentFake` is the ordinary fake with its writes kept in a ledger file of
its own -- never the service's database -- so that a write made by one
service process is still on the platform when the next process starts. It can
lose one answer on purpose: after committing a write, and the fault's
spending with it, durably, it raises `TimeoutError` instead of answering --
the platform wrote, the service never heard.

Every call that reads or writes corrections reloads the ledger under its lock,
and rebuilds what each invoice has given back from the writes themselves. A
ledger that cannot be read, or holds what this platform could not have
written, is refused, never reset. Its surface is the platform's calls; the
ordinary fake's test helpers that change state (`plant_action`,
`set_status`, `change_invoice`) are refused, since their effects would not
outlive the process.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.providers.base import ALLOCATIONS, Allocation, ProviderAction
from app.providers.fake.adapter import FakeBillingProvider
from evals import state

#: The one fault this double knows: an answer lost after the write commits.
TIMEOUT_AFTER_WRITE_ONCE = "timeout_after_write_once"


class LedgerRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The platform's ledger cannot be used as it stands; it is left as it is."""


class PersistentFake(FakeBillingProvider):
    def __init__(self, ledger: Path, *, fault: str | None = None) -> None:
        super().__init__()
        if fault not in (None, TIMEOUT_AFTER_WRITE_ONCE):
            raise ValueError(f"unknown fault {fault!r}")
        self.ledger = ledger
        self._fault_armed = fault == TIMEOUT_AFTER_WRITE_ONCE
        self._fault_spent = False
        with self._locked():
            self._reload()

    # -- the platform's calls, on the ledger as it stands ------------------

    async def plan_write(self, invoice_id: str, action_type: str) -> Allocation:
        with self._locked():
            self._reload()
        return await super().plan_write(invoice_id, action_type)

    async def find_action(self, idempotency_key: str, invoice_id: str) -> list[ProviderAction]:
        with self._locked():
            self._reload()
        return await super().find_action(idempotency_key, invoice_id)

    async def create_credit_note(self, *args, **kwargs) -> ProviderAction:
        return await self._write(super().create_credit_note, *args, **kwargs)

    async def create_refund(self, *args, **kwargs) -> ProviderAction:
        return await self._write(super().create_refund, *args, **kwargs)

    async def _write(self, call, *args, **kwargs) -> ProviderAction:
        """Reload, write, and commit a new write -- and the fault's spending,
        if this write spends it -- in one durable replace; then lose the
        answer if it did. A replay of a known key commits nothing."""
        lose = False
        with self._locked():
            self._reload()
            before = set(self._actions)
            action = await call(*args, **kwargs)
            if set(self._actions) != before:
                lose = self._fault_armed and not self._fault_spent
                self._fault_spent = self._fault_spent or lose
                self._commit()
        if lose:
            raise TimeoutError("the platform wrote, and its answer was lost")
        return action

    # -- the ledger -----------------------------------------------------------

    def writes(self) -> list[ProviderAction]:
        """What the ledger holds, read from the file."""
        with self._locked():
            return list(self._read()[0])

    def _reload(self) -> None:
        writes, spent = self._read()
        self._actions = {write.idempotency_key: write for write in writes}
        self._credited = {}
        self._refunded = {}
        for write in writes:
            given = self._credited if write.action_type == "credit_note" else self._refunded
            given[write.invoice_id] = given.get(write.invoice_id, 0) + write.amount_minor
        self._fault_spent = spent

    def _read(self) -> tuple[list[ProviderAction], bool]:
        if not self.ledger.exists():
            return [], False
        try:
            held = json.loads(self.ledger.read_text(encoding="utf-8"))
            if not isinstance(held, dict) or set(held) != {"writes", "fault_spent"}:
                raise ValueError("not a ledger")
            if not isinstance(held["fault_spent"], bool) or not isinstance(held["writes"], list):
                raise ValueError("fault_spent or writes")
            writes = [ProviderAction.model_validate(write) for write in held["writes"]]
        except (OSError, ValueError, TypeError) as error:
            raise LedgerRefused(
                f"the platform's ledger {self.ledger} cannot be used ({type(error).__name__}); "
                "it is never reset"
            ) from None
        problem = self._inconsistency(writes)
        if problem is not None:
            raise LedgerRefused(f"the platform's ledger {self.ledger} is inconsistent: {problem}")
        return writes, held["fault_spent"]

    def _inconsistency(self, writes: list[ProviderAction]) -> str | None:
        """What makes the ledger impossible for this platform, if anything:
        a key or a reference twice, an amount that is not positive, another
        provider, a live write, an allocation the instrument cannot take, an
        invoice the fixtures do not hold or in another currency."""
        if len({write.idempotency_key for write in writes}) != len(writes):
            return "a key twice"
        if len({write.provider_action_id for write in writes}) != len(writes):
            return "a reference twice"
        invoices = {invoice.id: invoice for invoice in self._invoices}
        for write in writes:
            invoice = invoices.get(write.invoice_id)
            if write.amount_minor <= 0:
                return f"{write.provider_action_id}: an amount that is not positive"
            if write.provider != self.name or not write.test_mode:
                return f"{write.provider_action_id}: not this test platform's"
            if write.allocation not in ALLOCATIONS[write.action_type]:
                return f"{write.provider_action_id}: an allocation the instrument cannot take"
            if invoice is None or invoice.total.currency != write.currency:
                return f"{write.provider_action_id}: an invoice the fixtures do not hold"
        return None

    # -- what this double does not keep -------------------------------------

    def plant_action(self, action: ProviderAction) -> None:
        raise NotImplementedError("the persistent fake keeps its own writes only")

    def set_status(self, provider_action_id: str, status: str) -> None:
        raise NotImplementedError("the persistent fake does not change a write's status")

    def change_invoice(self, invoice_id: str, **changes):
        raise NotImplementedError("the persistent fake does not change invoices")

    def _commit(self) -> None:
        state.write_atomically(
            self.ledger,
            {
                "writes": [write.model_dump(mode="json") for write in self._actions.values()],
                "fault_spent": self._fault_spent,
            },
        )

    @contextmanager
    def _locked(self, timeout: float = 30.0) -> Iterator[None]:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger.with_name(self.ledger.name + ".lock"), "a+b") as lock:
            deadline = time.monotonic() + timeout
            while not state.try_lock(lock):
                if time.monotonic() > deadline:
                    raise LedgerRefused(f"the platform's ledger {self.ledger} is held")
                time.sleep(0.02)
            try:
                yield
            finally:
                state.unlock(lock)

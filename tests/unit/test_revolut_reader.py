"""The Revolut Business reader against stubbed answers: no network
(specs/016-bank-evidence, FR-001, FR-002, FR-003a, FR-003b; plan 2a)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.domain.money import Money
from app.providers.bank_base import BankEvidence, BankUnavailable
from app.providers.revolut import mapping
from app.providers.revolut.adapter import RevolutBank, lower_bound, wire_instant
from app.providers.revolut.client import (
    HOST,
    WINDOW_REQUESTS,
    BudgetSpent,
    Pacer,
    RevolutConfigurationError,
    RevolutReader,
    load_signing_key,
    signature,
)

API_KEY = "test-key-not-real"
ACCOUNT = "acc_declared"
OTHER = "acc_other"
SIGNING = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
START = datetime(2026, 10, 10, tzinfo=UTC)
END = datetime(2026, 11, 14, 12, tzinfo=UTC)
NOW = END
LOWER = START - timedelta(days=7)
#: A secret-looking value in every text field: none of it may come out of the reader.
SECRET = "SECRET-BANK-TEXT"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def at(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def tx(tx_id, *, created, kind="topup", state="completed", amount=49, currency="EUR",
       account=ACCOUNT, completed="same", reference="Facture F-2026-0042", counterparty=None,
       related=None, **leg_extra):
    leg = {"leg_id": f"leg_{tx_id}", "account_id": account, "amount": amount,
           "currency": currency, "description": f"{SECRET} description",
           "balance": 1000, **leg_extra}
    if counterparty is not None:
        leg["counterparty"] = {"account_type": counterparty, "account_id": f"{SECRET}-cp",
                               "id": f"{SECRET}-cp-id"}
    item = {"id": tx_id, "type": kind, "state": state, "request_id": f"{SECRET}-req",
            "created_at": at(created), "updated_at": at(created),
            "merchant": {"name": f"{SECRET} merchant"}, "card": {"card_number": SECRET},
            "legs": [leg]}
    if completed == "same":
        item["completed_at"] = at(created + timedelta(seconds=1))
    elif completed is not None:
        item["completed_at"] = at(completed)
    if reference is not None:
        item["reference"] = reference
    if related is not None:
        item["related_transaction_id"] = related
    return item


def accounts_answer(*ids, currency="EUR"):
    return [{"id": i, "name": f"{SECRET} name", "currency": currency, "state": "active",
             "balance": 10, "public": False} for i in ids]


def answer(payload) -> httpx.Response:
    """JSON as Revolut writes it: a decimal goes out as a bare JSON number (through its
    shortest repr, which reads back as the same decimal)."""
    text = json.dumps(payload, default=lambda d: float(d) if isinstance(d, Decimal) else d)
    return httpx.Response(200, content=text.encode())


class Bank:
    """A stub of the Sandbox: `/accounts` and `/transactions`, honouring `from`
    (inclusive), `to` (exclusive) and `count`, newest first, ties in list order."""

    def __init__(self, transactions=(), accounts=None, *, ignore_bounds=False):
        self.transactions = list(transactions)
        self.accounts = accounts_answer(ACCOUNT) if accounts is None else accounts
        self.requests: list[httpx.Request] = []
        self.ignore_bounds = ignore_bounds
        self.override: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.override:
            return self.override[path]
        if path == "/api/1.0/accounts":
            return httpx.Response(200, json=self.accounts)
        if path == "/api/1.0/transactions":
            q = request.url.params
            low, high = mapping.instant(q["from"]), mapping.instant(q["to"])
            items = [t for t in self.transactions if t["legs"][0]["account_id"] == q["account"]]
            if not self.ignore_bounds:
                items = [t for t in items if low <= mapping.instant(t["created_at"]) < high]
            items.sort(key=lambda t: mapping.instant(t["created_at"]), reverse=True)
            return answer(items[: int(q["count"])])
        return httpx.Response(404)


def reader(bank, clock=None, wall_ms=lambda: 1763100000000):
    clock = clock or Clock()
    return RevolutReader(API_KEY, SIGNING, transport=httpx.MockTransport(bank),
                         pacer=Pacer(clock=clock, sleep=clock.sleep), clock=clock,
                         sleep=clock.sleep, wall_ms=wall_ms)


def revolut(bank, *, controlled=True, page_size=1000, clock=None, budget=20.0):
    return RevolutBank(reader(bank, clock), (ACCOUNT,), controlled=controlled,
                       page_size=page_size, budget=budget)


async def read(bank, **kwargs):
    return await revolut(bank, **kwargs).read(START, END, 7, NOW)


# -- the port ---------------------------------------------------------------------


def test_the_adapter_is_a_bank_evidence_port():
    assert isinstance(revolut(Bank()), BankEvidence)
    assert revolut(Bank()).name == "revolut"


# -- GET only, the pinned host, no redirects (FR-002) --------------------------------


async def test_every_request_is_a_signed_get_to_the_pinned_host():
    bank = Bank([tx("t1", created=START)])
    await read(bank)
    assert [r.url.path for r in bank.requests] == ["/api/1.0/accounts", "/api/1.0/transactions"]
    for request in bank.requests:
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == HOST
        assert request.headers["Revolut-Api-Timestamp"] == "1763100000000"
        assert "Revolut-Api-Signature" in request.headers


@pytest.mark.parametrize("method, url", [
    ("POST", "https://sandbox-b2b.revolut.com/api/1.0/transactions"),
    ("GET", "https://b2b.revolut.com/api/1.0/transactions"),
    ("GET", "http://sandbox-b2b.revolut.com/api/1.0/transactions"),
    ("GET", "https://sandbox-b2b.revolut.com:8443/api/1.0/transactions"),
    ("GET", "https://sandbox-b2b.revolut.com/elsewhere"),
])
async def test_anything_but_a_get_on_the_api_is_refused_before_it_leaves(method, url):
    bank = Bank()
    r = reader(bank)
    with pytest.raises(BankUnavailable) as raised:
        await r._client.request(method, url)
    assert raised.value.category == "request_refused"
    assert bank.requests == []


async def test_the_reader_has_no_way_to_write():
    public = {name for name in dir(RevolutReader) if not name.startswith("_")}
    assert public == {"aclose", "clock", "get"}


async def test_a_redirect_is_refused_never_followed():
    bank = Bank()
    bank.override["/api/1.0/accounts"] = httpx.Response(
        302, headers={"location": "https://elsewhere.example/api/1.0/accounts"})
    with pytest.raises(BankUnavailable) as raised:
        await read(bank)
    assert raised.value.category == "redirect"
    assert len(bank.requests) == 1


# -- the signature (FR-002) -----------------------------------------------------------


def test_the_signature_on_a_fixed_vector():
    query = "account=acc_1&from=2026-10-01T00%3A00%3A00.000Z&count=1000"
    produced = signature(SIGNING, "1763100000000", "GET", "/api/1.0/transactions", query, b"")
    assert produced == ("krxXwwwv9bvW3YqwTRHhcfyGn5A5fEe763/p4VIRMnSnx87muyksL46R6OHJsF6B"
                        "wGGejlJkoUon3xqmIMD4Ag==")
    SIGNING.public_key().verify(
        base64.b64decode(produced),
        b"1763100000000GET/api/1.0/transactions" + query.encode())


async def test_the_signature_covers_the_request_exactly_as_sent():
    bank = Bank([tx("t1", created=START)])
    await read(bank)
    for request in bank.requests:
        path, _, query = request.url.raw_path.decode().partition("?")
        message = (request.headers["Revolut-Api-Timestamp"] + "GET" + path + query).encode()
        SIGNING.public_key().verify(
            base64.b64decode(request.headers["Revolut-Api-Signature"]), message)


def test_the_signing_key_is_read_from_a_pem_file(tmp_path):
    pem = SIGNING.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    path = tmp_path / "key.pem"
    path.write_bytes(pem)
    loaded = load_signing_key(path)
    assert loaded.public_key().public_bytes_raw() == SIGNING.public_key().public_bytes_raw()


@pytest.mark.parametrize("content", [b"", b"not a key", None])
def test_an_unreadable_signing_key_fails_by_category(tmp_path, content):
    path = tmp_path / "key.pem"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(RevolutConfigurationError) as raised:
        load_signing_key(path)
    assert raised.value.category == "signing_key"
    assert str(path) not in str(raised.value)


# -- paging by created_at, the tie (FR-002, FR-003a) -----------------------------------


async def test_the_read_pages_from_the_margin_before_the_window():
    bank = Bank([tx("t1", created=START)])
    await read(bank)
    q = bank.requests[1].url.params
    assert q["account"] == ACCOUNT
    assert q["from"] == "2026-10-03T00:00:00.000Z"
    assert mapping.instant(q["to"]) > END
    assert q["count"] == "1000"


async def test_pages_move_to_the_last_created_at_and_read_everything():
    items = [tx(f"t{i}", created=LOWER + timedelta(days=1, minutes=i)) for i in range(7)]
    bank = Bank(items)
    result = await read(bank, page_size=3)
    assert result.complete
    assert sorted(c.transaction_id for c in result.credits) == sorted(t["id"] for t in items)
    pages = [r for r in bank.requests if r.url.path.endswith("/transactions")]
    # Three data pages and two boundary proofs; each next page ends at the last instant.
    assert len(pages) == 5
    assert pages[2].url.params["to"] == items[4]["created_at"]


async def test_a_full_page_ending_on_a_shared_instant_is_incomplete():
    same = LOWER + timedelta(days=2)
    items = [tx("t1", created=same + timedelta(minutes=1)), tx("t2", created=same),
             tx("t3", created=same), tx("t4", created=same - timedelta(minutes=1))]
    result = await read(Bank(items), page_size=3)
    assert not result.complete
    assert result.incompleteness == ("page_boundary_tie",)


async def test_a_tie_cut_by_the_page_and_alone_on_it_is_found():
    same = LOWER + timedelta(days=2)
    # The page holds t1, t2, t3; t4 shares t3's instant and is left out of it.
    items = [tx("t1", created=same + timedelta(minutes=2)),
             tx("t2", created=same + timedelta(minutes=1)),
             tx("t3", created=same), tx("t4", created=same)]
    result = await read(Bank(items), page_size=3)
    assert result.incompleteness == ("page_boundary_tie",)


async def test_a_full_page_not_newest_first_is_incomplete():
    items = [tx("t1", created=LOWER + timedelta(days=1)),
             tx("t2", created=LOWER + timedelta(days=3)),
             tx("t3", created=LOWER + timedelta(days=2))]
    bank = Bank(items)
    bank.override["/api/1.0/transactions"] = httpx.Response(200, json=items)
    result = await read(bank, page_size=3)
    assert result.incompleteness == ("page_boundary_tie",)


async def test_a_short_page_ends_the_read_complete():
    result = await read(Bank([tx("t1", created=START)]), page_size=3)
    assert result.complete and result.incompleteness == ()


# -- the rate budget ---------------------------------------------------------------


async def test_the_pacer_holds_sixty_requests_a_minute():
    clock = Clock()
    pacer = Pacer(clock=clock, sleep=clock.sleep)
    for _ in range(WINDOW_REQUESTS + 1):
        await pacer.wait(deadline=1000)
    assert clock.slept == [60.0]


async def test_a_read_that_would_wait_past_its_budget_is_incomplete():
    clock = Clock()
    pacer = Pacer(clock=clock, sleep=clock.sleep)
    for _ in range(WINDOW_REQUESTS - 1):  # the key's minute almost spent elsewhere
        await pacer.wait(deadline=1000)
    bank = Bank([tx("t1", created=START)])
    adapter = RevolutBank(
        RevolutReader(API_KEY, SIGNING, transport=httpx.MockTransport(bank), pacer=pacer,
                      clock=clock, sleep=clock.sleep),
        (ACCOUNT,), controlled=True)
    result = await adapter.read(START, END, 7, NOW)
    assert not result.complete
    assert result.incompleteness == ("budget_spent",)
    assert len(bank.requests) == 1  # the accounts only


async def test_retries_stop_at_the_budget():
    clock = Clock()
    bank = Bank()
    bank.override["/api/1.0/accounts"] = httpx.Response(429, headers={"retry-after": "30"})
    result = await read(bank, clock=clock, budget=20.0)
    assert result.incompleteness == ("budget_spent",)
    assert clock.slept == []


async def test_a_request_that_outlasts_the_budget_ends_the_read_incomplete():
    clock = Clock()
    bank = Bank([tx("t1", created=START)])

    def slow(request):
        clock.now += 25.0  # the answer comes after the 20-second budget
        return bank(request)

    result = await read(slow, clock=clock, budget=20.0)
    assert result.incompleteness == ("budget_spent",)
    assert len(bank.requests) == 1  # nothing asked once the budget was gone


async def test_each_request_is_bounded_by_what_remains_of_the_budget():
    clock = Clock()
    bank = Bank([tx("t1", created=START)])
    seen = []

    def timed(request):
        seen.append(request.extensions["timeout"]["read"])
        clock.now += 8.0
        return bank(request)

    await read(timed, clock=clock, budget=12.0)
    assert seen == [10.0, 4.0]


async def test_a_wait_for_the_pacer_is_cancelled_at_the_deadline():
    pacer = Pacer()  # real time
    await pacer._lock.acquire()  # another read holds the key's pacer
    bank = Bank([tx("t1", created=START)])
    adapter = RevolutBank(
        RevolutReader(API_KEY, SIGNING, transport=httpx.MockTransport(bank), pacer=pacer),
        (ACCOUNT,), controlled=True, budget=0.05)
    result = await asyncio.wait_for(adapter.read(START, END, 7, NOW), timeout=2)
    assert result.incompleteness == ("budget_spent",)
    assert bank.requests == []


async def test_a_slow_answer_is_cancelled_at_the_deadline():
    bank = Bank([tx("t1", created=START)])

    async def slow(request):
        await asyncio.sleep(5)
        return bank(request)

    adapter = RevolutBank(
        RevolutReader(API_KEY, SIGNING, transport=httpx.MockTransport(slow), pacer=Pacer()),
        (ACCOUNT,), controlled=True, budget=0.05)
    result = await asyncio.wait_for(adapter.read(START, END, 7, NOW), timeout=2)
    assert result.incompleteness == ("budget_spent",)


async def test_the_pacer_gives_no_slot_past_the_deadline():
    clock = Clock()
    clock.now = 5.0
    with pytest.raises(BudgetSpent):
        await Pacer(clock=clock, sleep=clock.sleep).wait(deadline=5.0)


@pytest.mark.parametrize("status, category", [
    (401, "authentication"), (403, "permission"), (400, "rejected"), (404, "rejected"),
    (500, "unreachable"), (429, "rate_limited"),
])
async def test_a_failed_answer_is_unavailable_by_category(status, category):
    bank = Bank()
    bank.override["/api/1.0/accounts"] = httpx.Response(
        status, text=f"{SECRET} error", headers={"retry-after": "0"})
    with pytest.raises(BankUnavailable) as raised:
        await read(bank, budget=1000)
    assert raised.value.category == category
    assert SECRET not in str(raised.value) and raised.value.__cause__ is None


async def test_no_answer_is_unavailable():
    def handler(request):
        raise httpx.ConnectError(f"{SECRET} connect")

    with pytest.raises(BankUnavailable) as raised:
        await read(handler, budget=1000)
    assert raised.value.category == "unreachable"
    assert SECRET not in str(raised.value)


# -- the declared accounts (FR-002) ---------------------------------------------------


async def test_the_declared_account_is_checked_on_every_read():
    bank = Bank([tx("t1", created=START)])
    adapter = revolut(bank)
    await adapter.read(START, END, 7, NOW)
    await adapter.read(START, END, 7, NOW)
    assert [r.url.path for r in bank.requests].count("/api/1.0/accounts") == 2


async def test_an_account_not_visible_is_not_read_and_the_read_is_incomplete():
    bank = Bank([tx("t1", created=START)], accounts=accounts_answer(OTHER))
    result = await read(bank)
    assert result.accounts[0].visible is False and result.accounts[0].currency is None
    assert result.incompleteness == ("account_not_visible",)
    assert result.credits == ()
    assert [r.url.path for r in bank.requests] == ["/api/1.0/accounts"]


async def test_the_account_currency_is_carried():
    result = await read(Bank(accounts=accounts_answer(ACCOUNT, currency="GBP")))
    assert result.accounts[0].currency == "GBP" and result.accounts[0].visible


async def test_another_accounts_transactions_are_never_kept():
    bank = Bank([tx("t1", created=START, account=OTHER)], ignore_bounds=True)
    bank.override["/api/1.0/transactions"] = httpx.Response(
        200, json=[tx("t1", created=START, account=OTHER)])
    result = await read(bank)
    assert result.credits == () and result.returns == ()


# -- mapping: decimals, types, origins, returns ----------------------------------------


async def test_amounts_are_decimals_never_floats():
    bank = Bank()
    raw = json.dumps([tx("t1", created=START, amount=0)]).replace(
        '"amount": 0', '"amount": 0.1')  # 0.1 is not exact as a binary float
    bank.override["/api/1.0/transactions"] = httpx.Response(200, content=raw.encode())
    result = await read(bank)
    assert result.credits[0].amount == Money(amount_minor=10, currency="EUR")


async def test_an_amount_finer_than_the_minor_unit_is_malformed_not_rounded():
    bank = Bank()
    raw = json.dumps([tx("t1", created=START, amount=0)]).replace(
        '"amount": 0', '"amount": 49.005')
    bank.override["/api/1.0/transactions"] = httpx.Response(200, content=raw.encode())
    with pytest.raises(BankUnavailable) as raised:
        await read(bank)
    assert raised.value.category == "malformed_answer"


@pytest.mark.parametrize("answer", [
    b"{not json", b'{"items": []}', b'[1, 2]', b'[{"id": "t1"}]',
    b'[{"id": "t1", "created_at": "yesterday"}]',
    b'[{"id": "t1", "created_at": "2026-10-10T00:00:00"}]',  # no time zone
    json.dumps([tx("t1", created=START)]).replace('"amount": 49', '"amount": NaN').encode(),
    json.dumps([tx("t1", created=START)]).replace('"amount": 49', '"amount": "49"').encode(),
    json.dumps([tx("t1", created=START)]).replace('"amount": 49', '"amount": true').encode(),
    json.dumps([tx("t1", created=START, currency="EURO")]).encode(),
    json.dumps([{**tx("t1", created=START), "legs": "one"}]).encode(),
    json.dumps([{**tx("t1", created=START), "type": []}]).encode(),
    json.dumps([{**tx("t1", created=START), "state": {"x": 1}}]).encode(),
    json.dumps([tx("t1", created=START, account=["acc"])]).encode(),
    json.dumps([tx("t1", created=START, completed=None)]).encode(),  # completed, no instant
    json.dumps([tx("r1", created=START, kind="topup_return", amount=-1,
                   completed=None)]).encode(),
    json.dumps([tx("t1", created=START),
                {**tx("t1", created=START), "state": "reverted"}]).encode(),
])
async def test_a_malformed_answer_is_unavailable_with_a_fixed_category(answer):
    bank = Bank()
    bank.override["/api/1.0/transactions"] = httpx.Response(200, content=answer)
    with pytest.raises(BankUnavailable) as raised:
        await read(bank)
    assert raised.value.category == "malformed_answer"
    assert raised.value.__cause__ is None


async def test_a_counterparty_of_an_unreadable_type_is_an_unknown_origin():
    item = tx("t1", created=START)
    item["legs"][0]["counterparty"] = {"account_type": ["external"]}
    result = await read(Bank([item]))
    assert result.credits[0].origin == "unknown"


async def test_the_same_transaction_answered_differently_across_pages_is_malformed():
    items = [tx(f"t{i}", created=LOWER + timedelta(days=1, minutes=i)) for i in range(4)]
    bank = Bank(items)
    calls = []

    def handler(request):
        calls.append(request)
        response = bank(request)
        if len([c for c in calls if c.url.path.endswith("/transactions")]) == 3:
            # The second data page repeats t3, now reverted.
            body = json.loads(response.content)
            body.append({**items[3], "state": "reverted"})
            return answer(body)
        return response

    with pytest.raises(BankUnavailable) as raised:
        await read(handler, page_size=3)
    assert raised.value.category == "malformed_answer"


async def test_the_boundary_proof_answering_differently_is_malformed():
    items = [tx(f"t{i}", created=LOWER + timedelta(days=1, minutes=i)) for i in range(4)]
    bank = Bank(items)

    def handler(request):
        response = bank(request)
        q = request.url.params
        if request.url.path.endswith("/transactions") and q["from"] == items[1]["created_at"]:
            return answer([{**items[1], "state": "reverted"}])
        return response

    with pytest.raises(BankUnavailable) as raised:
        await read(handler, page_size=3)
    assert raised.value.category == "malformed_answer"


async def test_malformed_accounts_are_unavailable():
    bank = Bank(accounts=[{"id": ACCOUNT, "currency": 12}])
    with pytest.raises(BankUnavailable) as raised:
        await read(bank)
    assert raised.value.category == "malformed_answer"


@pytest.mark.parametrize("kind", ["topup", "transfer"])
@pytest.mark.parametrize("counterparty, origin", [
    (None, "unknown"), ("external", "external"), ("self", "internal"),
    ("revolut", "internal"), ("something_new", "unknown"),
])
async def test_both_allowlisted_types_are_kept_with_their_origin(kind, counterparty, origin):
    result = await read(Bank([tx("t1", created=START, kind=kind, counterparty=counterparty)]))
    (credit,) = result.credits
    assert credit.type == kind and credit.origin == origin


@pytest.mark.parametrize("kind", ["card_refund", "card_credit", "exchange", "charge_refund",
                                  "tax_refund", "card_payment"])
async def test_other_types_are_not_credits(kind):
    result = await read(Bank([tx("t1", created=START, kind=kind)]))
    assert result.credits == ()


@pytest.mark.parametrize("state, kept", [
    ("completed", True), ("reverted", True), ("pending", False), ("created", False),
    ("declined", False), ("failed", False),
])
async def test_credits_are_completed_or_reverted(state, kept):
    completed = "same" if state == "completed" else None
    result = await read(Bank([tx("t1", created=START, state=state, completed=completed)]))
    assert bool(result.credits) is kept


async def test_a_reverted_credit_without_completed_at_is_kept():
    result = await read(Bank([tx("t1", created=START, state="reverted", completed=None)]))
    (credit,) = result.credits
    assert credit.state == "reverted" and credit.completed_at is None
    assert credit.created_at == START


async def test_debits_and_multi_leg_movements_are_not_credits():
    debit = tx("t1", created=START, kind="transfer", amount=-49)
    multi = tx("t2", created=START)
    multi["legs"].append({**multi["legs"][0], "leg_id": "l2", "account_id": OTHER,
                          "amount": -49})
    result = await read(Bank([debit, multi]))
    assert result.credits == ()


async def test_fx_and_fee_are_reported():
    items = [tx("t1", created=START, bill_amount=Decimal("52.10"), bill_currency="USD"),
             tx("t2", created=START + timedelta(minutes=1), bill_amount=Decimal("50")),
             tx("t3", created=START + timedelta(minutes=2), bill_currency="eur",
                bill_amount=49),
             tx("t4", created=START + timedelta(minutes=3), fee=Decimal("0.20")),
             tx("t5", created=START + timedelta(minutes=4), fee=0)]
    result = await read(Bank(items))
    flags = {c.transaction_id: (c.fx, c.fee) for c in result.credits}
    assert flags == {"t1": (True, False), "t2": (True, False), "t3": (False, False),
                     "t4": (False, True), "t5": (False, False)}


async def test_returns_related_or_of_a_return_type_with_their_own_instants():
    credit = tx("t1", created=START)
    related = tx("r1", created=START + timedelta(days=1), kind="transfer", amount=-49,
                 related="t1", reference=None, state="pending", completed=None)
    typed = tx("r2", created=START + timedelta(days=2), kind="topup_return", amount=-10,
               reference=None)
    refund = tx("r3", created=START + timedelta(days=3), kind="refund", amount=5)
    unrelated = tx("r4", created=START + timedelta(days=4), kind="card_payment",
                   amount=-3, related="t_unknown")
    result = await read(Bank([credit, related, typed, refund, unrelated]))
    found = {r.id: r for r in result.returns}
    assert set(found) == {"r1", "r2", "r3"}
    assert found["r1"].related_credit_id == "t1" and found["r1"].completed_at is None
    assert found["r1"].created_at == START + timedelta(days=1)
    assert found["r1"].amount == Money(amount_minor=-4900, currency="EUR")
    assert found["r2"].related_credit_id is None
    assert found["r2"].completed_at == START + timedelta(days=2, seconds=1)
    assert found["r3"].amount.amount_minor == 500


async def test_a_return_related_to_a_credit_on_another_page_is_found():
    credit = tx("t1", created=LOWER + timedelta(days=1))
    others = [tx(f"x{i}", created=LOWER + timedelta(days=2, minutes=i), kind="card_payment",
                 amount=-1) for i in range(3)]
    related = tx("r1", created=LOWER + timedelta(days=3), kind="transfer", amount=-49,
                 related="t1")
    result = await read(Bank([credit, *others, related]), page_size=3)
    assert [r.related_credit_id for r in result.returns] == ["t1"]


async def test_bank_text_is_never_kept_and_the_reference_never_serialised():
    result = await read(Bank([tx("t1", created=START, counterparty="external")]))
    (credit,) = result.credits
    assert credit.reference == "Facture F-2026-0042"  # in memory, for the check
    dumped = result.model_dump_json()
    assert SECRET not in dumped and "Facture" not in dumped
    assert SECRET not in repr(result)
    for field in ("description", "counterparty", "merchant", "card", "request_id", "balance"):
        assert field not in dumped


async def test_nothing_of_a_read_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    await read(Bank([tx("t1", created=START, counterparty="external")]))
    text = caplog.text
    assert API_KEY not in text and SECRET not in text and HOST not in text


# -- the window and coverage (FR-003a, FR-003b) ----------------------------------------


async def test_items_outside_the_window_are_dropped():
    bank = Bank([tx("late", created=END + timedelta(milliseconds=500))])
    result = await read(bank)
    assert result.credits == ()


def test_the_lower_bound_is_never_later_than_asked():
    start = datetime(2026, 10, 10, 8, 30, 15, 123456, tzinfo=UTC)
    bound = lower_bound(start, 7)
    assert bound <= start - timedelta(days=7)
    assert wire_instant(bound) == "2026-10-03T08:30:15.123Z"


async def test_coverage_is_established_on_controlled_worlds_with_nothing_early():
    result = await read(Bank([tx("t1", created=START)]), controlled=True)
    assert result.coverage_established and result.margin_days == 7


async def test_coverage_is_not_established_without_controlled_worlds():
    result = await read(Bank([tx("t1", created=START)]), controlled=False)
    assert result.complete and not result.coverage_established


async def test_coverage_is_not_established_when_something_read_was_created_early():
    early = tx("early", created=LOWER - timedelta(days=1), completed=START)
    bank = Bank([early, tx("t1", created=START)], ignore_bounds=True)
    result = await read(bank, controlled=True)
    assert not result.coverage_established
    assert [c.transaction_id for c in result.credits] == ["t1"]


async def test_the_read_carries_its_window_and_observation_instant():
    result = await read(Bank([tx("t1", created=START)]))
    assert (result.window_start, result.window_end) == (START, END)
    assert result.credits[0].observed_at == NOW


# -- the composition (plan 2a) ------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in ("BANK_EVIDENCE", "REVOLUT_API_KEY", "REVOLUT_SIGNING_KEY_PATH",
                 "REVOLUT_SANDBOX_ACCOUNT_ID", "REVOLUT_CONTROLLED_WORLDS", "REVOLUT_SEED_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "signing.pem"
    path.write_bytes(SIGNING.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))

    def configure(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    configure.key_path = str(path)
    return configure


def complete_revolut(env, **extra):
    env(BANK_EVIDENCE="revolut", REVOLUT_API_KEY=API_KEY,
        REVOLUT_SIGNING_KEY_PATH=env.key_path, REVOLUT_SANDBOX_ACCOUNT_ID=ACCOUNT, **extra)


def test_no_bank_configured_gives_none(env):
    from app.api.bank_config import bank_evidence

    assert bank_evidence() is None


def test_revolut_is_composed_from_its_variables(env):
    from app.api.bank_config import bank_evidence

    complete_revolut(env)
    bank = bank_evidence()
    assert isinstance(bank, RevolutBank) and bank._declared == (ACCOUNT,)
    assert bank._controlled is False


def test_controlled_worlds_only_by_the_exact_word(env):
    from app.api.bank_config import bank_evidence

    complete_revolut(env, REVOLUT_CONTROLLED_WORLDS="yes")
    assert bank_evidence()._controlled is False
    env(REVOLUT_CONTROLLED_WORLDS="enabled")
    assert bank_evidence()._controlled is True


@pytest.mark.parametrize("missing", ["REVOLUT_API_KEY", "REVOLUT_SIGNING_KEY_PATH",
                                     "REVOLUT_SANDBOX_ACCOUNT_ID"])
def test_a_missing_variable_fails_by_its_name(env, monkeypatch, missing):
    from app.api.bank_config import bank_evidence

    complete_revolut(env)
    monkeypatch.delenv(missing)
    with pytest.raises(RevolutConfigurationError) as raised:
        bank_evidence()
    assert missing in raised.value.category
    assert API_KEY not in str(raised.value)


def test_the_seed_key_in_the_service_environment_refuses_the_start(env):
    from app.api.bank_config import bank_evidence

    complete_revolut(env, REVOLUT_SEED_KEY="seed-key-not-real")
    with pytest.raises(RevolutConfigurationError) as raised:
        bank_evidence()
    assert raised.value.category == "seed_key_in_service_environment"
    assert "seed-key-not-real" not in str(raised.value)


def test_an_unknown_bank_is_refused_without_its_value(env):
    from app.api.bank_config import bank_evidence

    env(BANK_EVIDENCE="sk-pasted-here-by-mistake")
    with pytest.raises(RuntimeError) as raised:
        bank_evidence()
    assert "sk-pasted-here-by-mistake" not in str(raised.value)

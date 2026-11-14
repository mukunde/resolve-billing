"""The Revolut Sandbox seed's restrictions, one by one, against a stub: no network
(specs/016-bank-evidence, FR-006)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from scripts import revolut_seed as seed

KEY = "seed-key-not-real"
ACCOUNT = "acc_declared"
SECRET = "SECRET-ANSWER-TEXT"
ARGS = ["--invoice-number", "F-2026-0042", "--total", "49.00", "--other-number", "F-2026-0043"]


class Sandbox:
    def __init__(self, status=201, body=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body if body is not None else {"id": "tx_1", "state": "completed",
                                                   "note": SECRET}

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)


def seeder(sandbox, tmp_path):
    return seed.Seeder(KEY, ACCOUNT, seed.Intents(tmp_path / "intents.json"),
                       transport=httpx.MockTransport(sandbox))


def run(sandbox, tmp_path, argv=ARGS):
    return seed.run(seeder(sandbox, tmp_path), seed.arguments(argv))


def test_the_four_worlds_are_top_ups_on_the_declared_account(tmp_path):
    sandbox = Sandbox()
    lines = run(sandbox, tmp_path)
    assert lines == [f"{w}: made, state completed"
                     for w in ("total", "partial", "reverted", "other_number")]
    bodies = [json.loads(r.content, parse_float=Decimal) for r in sandbox.requests]
    assert [(b["reference"], b["amount"], b["state"]) for b in bodies] == [
        ("Facture F-2026-0042", Decimal("49.00"), "completed"),
        ("Facture F-2026-0042", Decimal("24.50"), "completed"),
        ("Facture F-2026-0042", Decimal("49.00"), "reverted"),
        ("Facture F-2026-0043", Decimal("49.00"), "completed"),
    ]
    for request, body in zip(sandbox.requests, bodies, strict=True):
        assert request.method == "POST"
        assert str(request.url) == "https://sandbox-b2b.revolut.com/api/1.0/sandbox/topup"
        assert body["account_id"] == ACCOUNT and body["currency"] == "EUR"
        assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_the_amount_is_a_json_number_never_a_string_or_a_float(tmp_path):
    sandbox = Sandbox()
    run(sandbox, tmp_path)
    assert b'"amount": 49.00}' in sandbox.requests[0].content


@pytest.mark.parametrize("method, url", [
    ("GET", seed.TOPUP_URL),
    ("POST", "https://sandbox-b2b.revolut.com/api/1.0/sandbox/transactions/tx_1/complete"),
    ("POST", "https://sandbox-b2b.revolut.com/api/1.0/transfer"),
    ("POST", "https://b2b.revolut.com/api/1.0/sandbox/topup"),
    ("POST", "http://sandbox-b2b.revolut.com/api/1.0/sandbox/topup"),
    ("POST", "https://sandbox-b2b.revolut.com:444/api/1.0/sandbox/topup"),
    ("POST", "https://sandbox-b2b.revolut.com/api/1.0/sandbox/topup?account=x"),
])
def test_any_other_request_is_refused_before_it_leaves(tmp_path, method, url):
    sandbox = Sandbox()
    s = seeder(sandbox, tmp_path)
    with pytest.raises(seed.Refused):
        s._client.request(method, url, content=json.dumps({"account_id": ACCOUNT}))
    assert sandbox.requests == []


@pytest.mark.parametrize("body", [
    {"account_id": "acc_other"}, {"amount": 1}, [ACCOUNT],
])
def test_a_top_up_to_any_other_account_is_refused(tmp_path, body):
    sandbox = Sandbox()
    s = seeder(sandbox, tmp_path)
    with pytest.raises(seed.Refused):
        s._client.post(seed.TOPUP_URL, content=json.dumps(body))
    assert sandbox.requests == []


def test_a_redirect_is_never_followed(tmp_path):
    sandbox = Sandbox(status=307, body={"note": SECRET})
    sandbox_redirect = httpx.MockTransport(lambda r: (
        sandbox.requests.append(r),
        httpx.Response(307, headers={"location": "https://elsewhere.example/x"}))[1])
    s = seed.Seeder(KEY, ACCOUNT, seed.Intents(tmp_path / "i.json"), transport=sandbox_redirect)
    lines = seed.run(s, seed.arguments(ARGS))
    # One request per world, none to the redirect's target; each outcome unknown.
    assert len(sandbox.requests) == 4
    assert {r.url.host for r in sandbox.requests} == {seed.HOST}
    assert lines[0] == "total: REPORTED -- redirected; the top-up's outcome is unknown"


def test_neither_the_key_nor_an_answer_is_ever_printed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("REVOLUT_SEED_KEY", KEY)
    monkeypatch.setenv("REVOLUT_SANDBOX_ACCOUNT_ID", ACCOUNT)
    sandbox = Sandbox(status=422, body={"message": SECRET})
    monkeypatch.setattr(seed, "INTENTS", tmp_path / "intents.json")
    real = seed.Seeder

    def stubbed(key, account, intents):
        return real(key, account, intents, transport=httpx.MockTransport(sandbox))

    monkeypatch.setattr(seed, "Seeder", stubbed)
    assert seed.main(ARGS) == 0
    out = capsys.readouterr()
    assert KEY not in out.out + out.err and SECRET not in out.out + out.err
    assert "total: refused (422)" in out.out


def test_a_missing_key_stops_before_anything(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("REVOLUT_SEED_KEY", raising=False)
    monkeypatch.setenv("REVOLUT_SANDBOX_ACCOUNT_ID", ACCOUNT)
    assert seed.main(ARGS) == 2
    assert "missing" in capsys.readouterr().out


def test_an_unknown_state_is_not_printed(tmp_path):
    lines = run(Sandbox(body={"state": SECRET}), tmp_path)
    assert lines[0] == "total: made, state other"


def test_a_rerun_skips_what_was_made(tmp_path):
    first = Sandbox()
    run(first, tmp_path)
    again = Sandbox()
    lines = run(again, tmp_path)
    assert again.requests == []
    assert all(line.endswith("already made") for line in lines)


def test_an_unknown_outcome_is_never_sent_again(tmp_path):
    def lost(request):
        if json.loads(request.content, parse_float=Decimal)["amount"] == Decimal("24.50"):
            return httpx.Response(201, json={"state": "completed"})
        raise httpx.ReadTimeout("lost", request=request)

    lines = run(lost, tmp_path)
    assert lines[0] == "total: REPORTED -- no answer; the top-up's outcome is unknown"
    again = Sandbox()
    lines = run(again, tmp_path)
    assert lines[0].startswith("total: REPORTED -- an earlier top-up's outcome is unknown")
    assert lines[1] == "partial: already made"
    assert again.requests == []  # three outcomes unknown, one made: nothing is sent


def test_a_refusal_by_revolut_can_be_retried(tmp_path):
    run(Sandbox(status=400, body={"message": SECRET}), tmp_path)
    again = Sandbox()
    run(again, tmp_path)
    assert len(again.requests) == 4


@pytest.mark.parametrize("argv", [
    ["--invoice-number", "A", "--total", "0.50", "--other-number", "B"],
    ["--invoice-number", "A", "--total", "10000.01", "--other-number", "B"],
    ["--invoice-number", "A", "--total", "49.001", "--other-number", "B"],
    ["--invoice-number", "A", "--total", "NaN", "--other-number", "B"],
    ["--invoice-number", "A", "--total", "49.00", "--other-number", "A"],
])
def test_bad_arguments_are_refused(argv):
    with pytest.raises(SystemExit):
        seed.arguments(argv)


def test_the_seed_never_reads_the_service_configuration():
    source = Path(seed.__file__).read_text(encoding="utf-8")
    assert "REVOLUT_API_KEY" not in source and "load_dotenv" not in source
    assert "REVOLUT_SIGNING_KEY_PATH" not in source

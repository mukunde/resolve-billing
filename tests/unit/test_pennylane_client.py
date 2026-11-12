"""Pennylane's client, against a stub transport: no network
(specs/014-pennylane-read, FR-001, FR-007, FR-008)."""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import NotFound, ProviderUnavailable
from app.providers.pennylane.client import WINDOW_REQUESTS, Pacer, PennylaneReader
from app.providers.pennylane.keys import ProviderConfigurationError

SANDBOX = 282871
TOKEN = "test-token-not-real"


def me(company_id=SANDBOX):
    return httpx.Response(200, json={"company": {"id": company_id}})


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def reader(handler, company_id=SANDBOX, clock=None):
    clock = clock or Clock()
    return PennylaneReader(
        TOKEN,
        company_id,
        transport=httpx.MockTransport(handler),
        pacer=Pacer(clock=clock, sleep=clock.sleep),
        sleep=clock.sleep,
    )


def routes(table):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        answer = table[request.url.path.removeprefix("/api/external/v2")]
        return answer(request) if callable(answer) else answer

    return handler, calls


class TestTheCompanyCheck:
    async def test_the_declared_sandbox_is_read(self):
        handler, calls = routes({"/me": me(), "/customers/1": httpx.Response(200, json={"id": 1})})
        assert (await reader(handler).get("/customers/1")) == {"id": 1}

    @pytest.mark.parametrize("answer", [me(999), httpx.Response(200, json={}),
                                        httpx.Response(200, json=[1])])
    async def test_another_company_or_an_unreadable_identity_reads_nothing(self, answer):
        handler, calls = routes({"/me": answer, "/customers/1": httpx.Response(200, json={})})
        client = reader(handler)
        for _ in range(2):  # the verdict is kept: every later call refused too
            with pytest.raises(ProviderConfigurationError, match="sandbox"):
                await client.get("/customers/1")
        assert [c.url.path for c in calls] == ["/api/external/v2/me"]

    async def test_the_token_is_sent_as_a_bearer_and_never_in_an_error(self):
        handler, calls = routes({"/me": httpx.Response(401)})
        with pytest.raises(ProviderConfigurationError) as raised:
            await reader(handler).get("/customers/1")
        assert raised.value.category == "authentication"
        assert TOKEN not in str(raised.value)
        assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "expected", "category"),
        [(401, ProviderConfigurationError, "authentication"),
         (403, ProviderConfigurationError, "permission"),
         (422, ProviderConfigurationError, "invalid_request")],
    )
    async def test_by_category(self, status, expected, category):
        handler, _ = routes({"/me": me(), "/x": httpx.Response(status, text="secret body")})
        with pytest.raises(expected) as raised:
            await reader(handler).get("/x")
        assert raised.value.category == category
        assert "secret" not in str(raised.value)

    async def test_a_404_is_not_found(self):
        handler, _ = routes({"/me": me(), "/x": httpx.Response(404)})
        with pytest.raises(NotFound):
            await reader(handler).get("/x")

    async def test_a_429_waits_what_it_asks_then_succeeds(self):
        answers = iter([httpx.Response(429, headers={"retry-after": "3"}),
                        httpx.Response(200, json={"ok": True})])
        handler, _ = routes({"/me": me(), "/x": lambda request: next(answers)})
        clock = Clock()
        assert (await reader(handler, clock=clock).get("/x")) == {"ok": True}
        assert 3.0 in clock.slept

    async def test_failures_past_the_attempts_are_unavailable(self):
        handler, _ = routes({"/me": me(), "/x": httpx.Response(503, text="body")})
        with pytest.raises(ProviderUnavailable) as raised:
            await reader(handler).get("/x")
        assert "body" not in str(raised.value)

    async def test_no_answer_is_unavailable(self):
        def handler(request):
            if request.url.path.endswith("/me"):
                return me()
            raise httpx.ConnectError("secret detail")

        with pytest.raises(ProviderUnavailable) as raised:
            await reader(handler).get("/x")
        assert "secret" not in str(raised.value)


class TestPagination:
    async def test_every_page_to_the_end_with_the_filter_resent(self):
        pages = {
            None: {"items": [{"id": 1}], "has_more": True, "next_cursor": "c1"},
            "c1": {"items": [{"id": 2}], "has_more": False, "next_cursor": None},
        }

        def page(request):
            assert request.url.params["filter"] == "f"
            return httpx.Response(200, json=pages[request.url.params.get("cursor")])

        handler, _ = routes({"/me": me(), "/l": page})
        assert await reader(handler).every("/l", {"filter": "f"}) == [{"id": 1}, {"id": 2}]

    @pytest.mark.parametrize(
        "page",
        [
            {"items": "x", "has_more": False},
            {"items": [1], "has_more": False},
            {"items": [], "has_more": True, "next_cursor": None},
            {"items": [], "has_more": None},
        ],
    )
    async def test_a_malformed_page_is_unavailable(self, page):
        handler, _ = routes({"/me": me(), "/l": httpx.Response(200, json=page)})
        with pytest.raises(ProviderUnavailable):
            await reader(handler).every("/l")

    async def test_a_repeating_cursor_is_unavailable(self):
        handler, _ = routes({"/me": me(), "/l": httpx.Response(
            200, json={"items": [], "has_more": True, "next_cursor": "same"})})
        with pytest.raises(ProviderUnavailable):
            await reader(handler).every("/l")


class TestThePacer:
    async def test_no_more_than_the_window_allows(self):
        clock = Clock()
        pacer = Pacer(clock=clock, sleep=clock.sleep)
        for _ in range(WINDOW_REQUESTS):
            await pacer.wait()
        assert clock.slept == []
        await pacer.wait()
        assert clock.slept and clock.now >= 5.0

    def test_one_pacer_per_token(self):
        assert Pacer.for_token("a") is Pacer.for_token("a")
        assert Pacer.for_token("a") is not Pacer.for_token("b")

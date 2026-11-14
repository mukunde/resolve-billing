"""Make the bank worlds on the Revolut Business Sandbox (specs/016-bank-evidence, FR-006,
SC-002).

    python -m dotenv -f .env.seed run -- python -m scripts.revolut_seed \\
        --invoice-number <cited number> --total <the invoice's total, e.g. 49.00> \\
        --other-number <another loaded invoice's number> [--currency EUR]

Four top-ups on the declared account: one citing the invoice's number for its total, one
for part of it, one `reverted`, one citing the other number. The maintainer chooses the
invoice the Sandbox run (plan section 3) checks against; the seed is told its number and
total and nothing else.

`.env.seed` holds `REVOLUT_SEED_KEY` (the Sandbox's full-access key) and
`REVOLUT_SANDBOX_ACCOUNT_ID`; never `.env`, which the service loads, and the service
refuses to start with the seed key in its environment. Because that key can do anything
on the Sandbox, the seed:

- sends **only** `POST /sandbox/topup`, to the pinned Sandbox origin, by HTTPS, with the
  declared account's id, and refuses any other request before it leaves;
- never follows a redirect: one is a failure;
- never prints, logs or raises the key, a header or a response body: each world ends in a
  fixed line, its state read back from a closed set.

One-shot: an intent is noted before each top-up is sent and marked made once Revolut
answers; a rerun skips what was made and stops where an earlier send's outcome is
unknown (a top-up is never sent twice automatically). The Sandbox's states are final, so a
world, once made, stays as it was made.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

HOST = "sandbox-b2b.revolut.com"
TOPUP_PATH = "/api/1.0/sandbox/topup"
TOPUP_URL = f"https://{HOST}{TOPUP_PATH}"
#: Revolut's ceiling on one Sandbox top-up.
TOPUP_MAX = Decimal("10000")
#: The states a top-up is answered with, printed as such; anything else is "other".
STATES = frozenset({"pending", "completed", "reverted", "failed", "declined", "created"})

#: Where intents are noted between runs: outside any checkout.
INTENTS = Path.home() / ".resolve-billing" / "revolut-seed-intents.json"

# Nothing of a request -- its URL, its headers -- reaches a log.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.CRITICAL + 1)
    logging.getLogger(_name).propagate = False


class Refused(Exception):  # noqa: N818 -- a request the seed will not send
    """A request other than the one top-up the seed may send; nothing was sent."""


class Stop(Exception):  # noqa: N818 -- the seed cannot tell, and says so
    pass


@dataclass(frozen=True)
class World:
    name: str
    reference: str
    amount: Decimal
    state: str


def worlds(number: str, total: Decimal, other: str) -> tuple[World, ...]:
    """The four worlds of plan 2a: the total, a part, reverted, another number."""
    part = (total / 2).quantize(Decimal("0.01"))
    return (
        World("total", f"Facture {number}", total, "completed"),
        World("partial", f"Facture {number}", part, "completed"),
        World("reverted", f"Facture {number}", total, "reverted"),
        World("other_number", f"Facture {other}", total, "completed"),
    )


@dataclass
class Intents:
    """Top-ups noted before they are sent (`sent`), marked once answered (`made`)."""

    path: Path

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def state(self, key: str) -> str | None:
        return self._load().get(key)

    def mark(self, key: str, state: str) -> None:
        current = self._load()
        current[key] = state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")

    def clear(self, key: str) -> None:
        current = self._load()
        if current.pop(key, None) is not None:
            self.path.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")

    @contextmanager
    def lock(self) -> Iterator[None]:
        """One seed run at a time: two runs could both find no intent and both send. A
        lock left by a run that died is removed by hand, once the Sandbox is checked."""
        path = self.path.with_name(self.path.name + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            raise Stop(f"another seed run holds {path}; remove it by hand if none runs") from None
        try:
            yield
        finally:
            path.unlink(missing_ok=True)


def _guard(account_id: str):
    def guard(request: httpx.Request) -> None:
        """The last gate before a request leaves: one method, one URL, one account."""
        url = request.url
        if (
            request.method != "POST"
            or url.scheme != "https"
            or url.host != HOST
            or url.port not in (None, 443)
            or url.raw_path != TOPUP_PATH.encode("ascii")
        ):
            raise Refused("not the top-up request")
        try:
            body = json.loads(request.content, parse_float=Decimal)
        except ValueError:
            raise Refused("an unreadable body") from None
        if not isinstance(body, dict) or body.get("account_id") != account_id:
            raise Refused("not the declared account")

    return guard


def _body(account_id: str, world: World, currency: str) -> bytes:
    """The top-up's JSON, its amount a JSON number written from the decimal (never a
    float)."""
    fields = json.dumps({"account_id": account_id, "currency": currency,
                         "reference": world.reference, "state": world.state})
    return (fields[:-1] + f', "amount": {world.amount}' + "}").encode("utf-8")


class Seeder:
    def __init__(self, key: str, account_id: str, intents: Intents, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._account = account_id
        self.intents = intents
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "application/json"},
            transport=transport,
            follow_redirects=False,
            timeout=15.0,
            event_hooks={"request": [_guard(account_id)]},
        )

    def close(self) -> None:
        self._client.close()

    def topup(self, world: World, currency: str, label: str) -> str:
        """One world: skipped when made, refused when an earlier send is unknown."""
        key = f"{self._account}|{label}/{world.name}"
        known = self.intents.state(key)
        if known == "made":
            return "already made"
        if known == "sent":
            raise Stop("an earlier top-up's outcome is unknown; check the Sandbox by hand")
        self.intents.mark(key, "sent")
        try:
            response = self._client.post(TOPUP_URL, content=_body(self._account, world, currency))
        except Refused:
            self.intents.clear(key)  # refused before it left: nothing was sent
            raise
        except httpx.HTTPError:
            raise Stop("no answer; the top-up's outcome is unknown") from None
        status = response.status_code
        if 300 <= status < 400:
            raise Stop("redirected; the top-up's outcome is unknown")
        if status in (200, 201):
            self.intents.mark(key, "made")
            try:
                state = json.loads(response.content).get("state")
            except (ValueError, AttributeError):
                return "made (state unreadable)"
            return f"made, state {state if state in STATES else 'other'}"
        if 400 <= status < 500 and status != 429:
            self.intents.clear(key)  # refused by Revolut: nothing was made
            return f"refused ({status})"
        # 429, 5xx, or a success other than 200/201 (202, 204...): whether a top-up
        # was made is unknown, and the intent stays.
        raise Stop(f"answered {status}; the top-up's outcome is unknown")


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("not a decimal") from None
    if not number.is_finite() or number < 1 or number > TOPUP_MAX or number != round(number, 2):
        raise argparse.ArgumentTypeError("an amount from 1 to 10000, two decimals at most")
    # Canonical, so `49`, `49.0` and `49.00` are one world and one intent.
    return number.quantize(Decimal("0.01"))


def _currency(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{3}", value.strip()):
        raise argparse.ArgumentTypeError("a three-letter currency code")
    return value.strip().upper()


def _number(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("an invoice number")
    return value.strip()


def arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="revolut_seed")
    parser.add_argument("--invoice-number", required=True, type=_number)
    parser.add_argument("--total", required=True, type=_decimal)
    parser.add_argument("--other-number", required=True, type=_number)
    parser.add_argument("--currency", default="EUR", type=_currency)
    args = parser.parse_args(argv)
    if args.invoice_number.casefold() == args.other_number.casefold():
        parser.error("the other number must differ from the invoice's")
    return args


def run(seeder: Seeder, args: argparse.Namespace) -> list[str]:
    """Every world, under the lock; an intent's identity is the canonical arguments."""
    # Numbers case-folded: references are matched whatever their case, so `INV-A` and
    # `inv-a` are the same worlds.
    label = json.dumps([args.invoice_number.casefold(), str(args.total),
                        args.other_number.casefold(), args.currency])
    lines = []
    try:
        with seeder.intents.lock():
            for world in worlds(args.invoice_number, args.total, args.other_number):
                try:
                    lines.append(f"{world.name}: {seeder.topup(world, args.currency, label)}")
                except Stop as stop:
                    lines.append(f"{world.name}: REPORTED -- {stop}")
                except Refused as refusal:
                    lines.append(f"{world.name}: REFUSED before sending -- {refusal}")
    except Stop as stop:
        lines.append(f"stopped before any top-up: {stop}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = arguments(sys.argv[1:] if argv is None else argv)
    key = os.environ.get("REVOLUT_SEED_KEY", "")
    account = os.environ.get("REVOLUT_SANDBOX_ACCOUNT_ID", "")
    if not key or not account:
        print("revolut_seed: missing REVOLUT_SEED_KEY or REVOLUT_SANDBOX_ACCOUNT_ID")
        return 2
    seeder = Seeder(key, account, Intents(INTENTS))
    try:
        for line in run(seeder, args):
            print(line)
    finally:
        seeder.close()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())

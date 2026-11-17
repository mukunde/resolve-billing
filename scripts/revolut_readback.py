"""Read the seeded worlds back from the Revolut Business Sandbox (specs/016-bank-evidence,
SC-002, plan section 3).

    python -m dotenv -f <path to .env> run -- python -m scripts.revolut_readback \\
        --invoice-number RB-SC002-01 --total 49.00 --other-number RB-SC002-02 \\
        --issued-at 2026-11-15T00:00:00Z

The service's own composition (`app/api/bank_config.py`), read-only and signed, with
`BANK_EVIDENCE=revolut` and `REVOLUT_CONTROLLED_WORLDS=enabled` set for this run alone:
the seeded account is a controlled world. It refuses to run with `REVOLUT_SEED_KEY` in
its environment -- never load `.env.seed` here.

The invoices are fictitious (the maintainer's choice, 2026-11-15): the invoice the seed
cited, for its total, issued at `--issued-at`, and the other number, as two invoices of
one customer. The window runs from the issue to now, with the 7-day margin.

**Structure only** is printed: the read's completeness and coverage, the declared
account's visibility and currency, and per credit its type, state, amount, currency,
origin, FX and fee flags, whether it has a completion instant, and which of the two
fictitious numbers its reference cites -- never the reference's text, an id, a payer or
a description. Returns: type, signed amount, whether related to a credit read. Then the
bank check on the cited invoice, as the investigation runs it (`bank_credit_check`): its
outcome, reasons and finding code.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.domain.models import Invoice
from app.domain.money import Money
from app.domain.references import cited_invoices
from app.domain.rules import BANK_MARGIN_DAYS, bank_credit_check

#: Every text printed comes from these closed sets; anything else prints as `other`, so
#: no bank-authored string -- or a terminal control sequence in it -- reaches the screen.
TYPES = frozenset({"topup", "transfer", "topup_return", "refund"})
STATES = frozenset({"created", "pending", "completed", "declined", "failed", "reverted"})
ORIGINS = frozenset({"external", "internal", "unknown"})
CURRENCY = re.compile(r"[A-Z]{3}")


def _closed(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def _currency(value: object) -> str:
    return value if isinstance(value, str) and CURRENCY.fullmatch(value) else "other"


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("an ISO 8601 instant") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("an instant with its offset, e.g. ...Z")
    return parsed.astimezone(UTC)


def _total(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("a decimal") from None
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise argparse.ArgumentTypeError("a positive amount, two decimals at most")
    return amount


def arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="revolut_readback")
    parser.add_argument("--invoice-number", required=True)
    parser.add_argument("--total", required=True, type=_total)
    parser.add_argument("--other-number", required=True)
    parser.add_argument("--currency", default="EUR")
    parser.add_argument("--issued-at", required=True, type=_instant)
    return parser.parse_args(argv)


def invoices(args: argparse.Namespace) -> list[Invoice]:
    def one(id_: str, number: str) -> Invoice:
        return Invoice(
            id=id_, customer_id="cus_sc002", subscription_id=None,
            total=Money(amount_minor=int(args.total * 100), currency=args.currency),
            status="open", issued_at=args.issued_at, lines=[], number=number,
        )

    return [one("sc002_invoice", args.invoice_number), one("sc002_other", args.other_number)]


def describe(read, loaded: list[Invoice]) -> list[str]:
    names = {"sc002_invoice": "the invoice", "sc002_other": "the other number"}
    lines = [
        f"read: complete={read.complete} incompleteness={list(read.incompleteness)} "
        f"coverage_established={read.coverage_established}",
    ]
    for account in read.accounts:
        lines.append(
            f"declared account: visible={account.visible} currency={_currency(account.currency)}"
        )
    lines.append(f"credits: {len(read.credits)}")
    for credit in sorted(read.credits, key=lambda c: c.created_at):
        cites = sorted(names[i] for i in cited_invoices(credit.reference, loaded))
        lines.append(
            f"- credit type={_closed(credit.type, TYPES)} state={_closed(credit.state, STATES)} "
            f"amount={Decimal(credit.amount.amount_minor) / 100} "
            f"{_currency(credit.amount.currency)} "
            f"origin={_closed(credit.origin, ORIGINS)} fx={credit.fx} fee={credit.fee} "
            f"completed_at={'present' if credit.completed_at else 'absent'} "
            f"reference={'present' if credit.reference else 'absent'} cites={cites}"
        )
    read_ids = {c.transaction_id for c in read.credits}
    lines.append(f"returns: {len(read.returns)}")
    for ret in read.returns:
        lines.append(
            f"- return type={_closed(ret.type, TYPES)} "
            f"amount={Decimal(ret.amount.amount_minor) / 100} {_currency(ret.amount.currency)} "
            f"related_to_a_credit_read={ret.related_credit_id in read_ids}"
        )
    return lines


def checked(read, loaded: list[Invoice], now: datetime) -> list[str]:
    """The bank check on the cited invoice, as the investigation runs it: its outcome,
    reason and reasons -- closed codes only -- and the finding's code."""
    finding, check = bank_credit_check(loaded[0], read, loaded, now)
    return [
        f"check: outcome={check.outcome} reason={check.reason} "
        f"reasons={list(check.reasons)} items={len(check.items)} "
        f"finding={finding.code.value if finding is not None else 'none'}"
    ]


async def readback(args: argparse.Namespace) -> list[str]:
    from app.api.bank_config import bank_evidence

    os.environ["BANK_EVIDENCE"] = "revolut"
    os.environ["REVOLUT_CONTROLLED_WORLDS"] = "enabled"
    bank = bank_evidence()
    now = datetime.now(UTC)
    read = await bank.read(args.issued_at, now, BANK_MARGIN_DAYS, now)
    loaded = invoices(args)
    return describe(read, loaded) + checked(read, loaded, now)


def main(argv: list[str] | None = None) -> int:
    args = arguments(sys.argv[1:] if argv is None else argv)
    from app.providers.bank.client import RevolutConfigurationError

    try:
        lines = asyncio.run(readback(args))
    except RevolutConfigurationError as error:
        # Its message names a variable or a condition, never a value.
        print(f"revolut_readback: configuration -- {error}")
        return 2
    except Exception as error:  # noqa: BLE001 -- its type and category only, never a text
        print(f"revolut_readback: {type(error).__name__} {getattr(error, 'category', '')}")
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())

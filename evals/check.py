"""Check that a case file and its fixtures fit together, without running it.

For a case file kept outside this repository, whose fixtures live in
``RESOLVE_EXTRA_FIXTURES_DIR``: it says whether the plumbing works -- the
fixtures load as one platform, every case has its fields, and every case's
references resolve the way its expectations say -- and nothing else. No
engine is called, no decision is made, and only totals are printed, so the
check can run on a holdout set without producing a result about it.

What "resolve the way its expectations say" means:

- a case expecting an unknown or suspected sender has an address that matches
  no customer; every other case has one that matches;
- a known sender's invoice belongs to that customer, unless the case expects
  ``UNKNOWN_INVOICE``, in which case it does not.

The 180-day lookback of the investigation is not applied: an invoice older
than that resolves here and not in a run.

Usage:
    python -m evals.runner --check-only --cases path/to/cases.json
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.domain.enums import RuleCode, SenderStatus
from app.providers.fake.fixtures import FixtureError, extra_fixtures_dir, load_all

REQUIRED = frozenset(
    {
        "id",
        "why",
        "message",
        "customer_email",
        "invoice_id",
        "expect_classification",
        "expect_action",
        "expect_human_approval",
        "expect_findings",
    }
)

_UNKNOWN_SENDERS = {SenderStatus.UNKNOWN.value, SenderStatus.SUSPECTED_IMPERSONATION.value}


def check(cases_path: Path) -> int:
    """Print totals; return 1 when anything does not fit."""
    try:
        extra = extra_fixtures_dir()
        data = load_all()
    except FixtureError as error:
        print(f"fixtures                      : error -- {error}")
        return 1

    print(f"extra fixtures directory      : {'set' if extra else 'not set'}")
    print(
        "fixtures loaded               : "
        f"{len(data.customers)} customers, {len(data.subscriptions)} subscriptions, "
        f"{len(data.invoices)} invoices, {len(data.payments)} payments, "
        f"{len(data.usage)} usage records"
    )

    try:
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"case file                     : unreadable -- {type(error).__name__}")
        return 1
    if not isinstance(cases, list):
        print("case file                     : not a list of cases")
        return 1

    customers = {c.email.lower(): c for c in data.customers}
    owner = {i.id: i.customer_id for i in data.invoices}

    well_formed = [c for c in cases if isinstance(c, dict) and c.keys() >= REQUIRED]
    malformed = len(cases) - len(well_formed)
    duplicated = sum(n - 1 for n in Counter(c["id"] for c in well_formed).values() if n > 1)
    resolved = sum(1 for c in well_formed if _fits(c, customers, owner))
    misfit = len(well_formed) - resolved

    print(f"cases loaded                  : {len(cases)}")
    print(f"cases missing a field         : {malformed}")
    print(f"duplicate case ids            : {duplicated}")
    print(f"references as expected        : {resolved}")
    print(f"references not as expected    : {misfit}")

    return 1 if malformed or duplicated or misfit else 0


def _fits(case: dict, customers: dict, owner: dict[str, str]) -> bool:
    email = (case["customer_email"] or "").strip().lower()
    customer = customers.get(email)
    if case.get("expect_sender") in _UNKNOWN_SENDERS:
        return customer is None
    if customer is None:
        return False
    invoice_id = case["invoice_id"]
    if invoice_id is None:
        return True
    belongs = owner.get(invoice_id) == customer.id
    expects_unknown = RuleCode.UNKNOWN_INVOICE.value in case["expect_findings"]
    return belongs != expects_unknown

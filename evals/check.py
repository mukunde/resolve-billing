"""Check that a case file and its fixtures fit together, without running it.

For a case file kept outside this repository, whose fixtures live in
``RESOLVE_EXTRA_FIXTURES_DIR``: it says whether the plumbing works and
nothing else. No engine is called, no decision is made, and only totals and
error categories are printed -- never a path, an id or a value from the data
-- so the check can run on a holdout set without producing anything about it.

What is checked:

- the fixtures load as one platform, and every link between them resolves (a
  subscription's customer, an invoice's customer and subscription, a
  payment's invoice, a usage record's subscription);
- every case has its fields, with the right types and known values;
- every case's references resolve the way it expects: a case expecting an
  unknown sender has an address matching no customer, and every other case
  one that matches; a case expecting a suspected impersonation triggers the
  impersonation rule itself, and a merely unknown sender does not; a known
  sender's invoice belongs to that customer, unless the case expects
  ``UNKNOWN_INVOICE``.

What is not: the expected decisions themselves (only a run says whether the
system reaches them), and the investigation's 180-day lookback (an invoice
older than that resolves here and not in a run).

Usage:
    python -m evals.runner --check-only --cases path/to/cases.json
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from app.decision.base import Classification
from app.domain.enums import RuleCode, SenderStatus
from app.domain.rules import suspected_impersonation
from app.providers.fake.fixtures import (
    DuplicateFixture,
    FixtureSet,
    MissingFixturesDirectory,
    extra_fixtures_dir,
    load_all,
)

ACTIONS = frozenset({"credit_note", "refund", "reply", "request_information", "none"})
_CLASSIFICATIONS = {c.value for c in Classification}
_FINDINGS = {c.value for c in RuleCode}
_SENDERS = {s.value for s in SenderStatus}
_UNKNOWN_SENDERS = {SenderStatus.UNKNOWN.value, SenderStatus.SUSPECTED_IMPERSONATION.value}


def check(cases_path: Path) -> int:
    """Print totals; return 1 when anything does not fit."""
    try:
        extra = extra_fixtures_dir()
        data = load_all()
    except MissingFixturesDirectory:
        return _fail("fixtures", "extra directory not found")
    except DuplicateFixture:
        return _fail("fixtures", "an entry is defined twice")
    except (OSError, ValueError, ArithmeticError, KeyError, TypeError, ValidationError):
        # Messages can quote the data; only the category is printed.
        return _fail("fixtures", "unreadable or invalid entry")

    print(f"extra fixtures directory      : {'set' if extra else 'not set'}")
    print(
        "fixtures loaded               : "
        f"{len(data.customers)} customers, {len(data.subscriptions)} subscriptions, "
        f"{len(data.invoices)} invoices, {len(data.payments)} payments, "
        f"{len(data.usage)} usage records"
    )
    orphans = _orphan_links(data)
    print(f"fixture links not resolved    : {orphans}")

    try:
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fail("case file", "unreadable")
    if not isinstance(cases, list):
        return _fail("case file", "not a list of cases")

    well_formed = [c for c in cases if _well_formed(c)]
    malformed = len(cases) - len(well_formed)
    duplicated = sum(n - 1 for n in Counter(c["id"] for c in well_formed).values() if n > 1)
    resolved = sum(1 for c in well_formed if _fits(c, data))
    misfit = len(well_formed) - resolved

    print(f"cases loaded                  : {len(cases)}")
    print(f"cases malformed               : {malformed}")
    print(f"duplicate case ids            : {duplicated}")
    print(f"references as expected        : {resolved}")
    print(f"references not as expected    : {misfit}")

    return 1 if orphans or malformed or duplicated or misfit else 0


def _fail(what: str, category: str) -> int:
    print(f"{what:<30}: error -- {category}")
    return 1


def _orphan_links(data: FixtureSet) -> int:
    customers = {c.id for c in data.customers}
    subscriptions = {s.id: s.customer_id for s in data.subscriptions}
    invoices = {i.id for i in data.invoices}
    links = [s.customer_id in customers for s in data.subscriptions]
    links += [i.customer_id in customers for i in data.invoices]
    # An invoice's subscription exists and belongs to the same customer.
    links += [
        subscriptions.get(i.subscription_id) == i.customer_id
        for i in data.invoices
        if i.subscription_id
    ]
    links += [p.invoice_id in invoices for p in data.payments if p.invoice_id]
    links += [u.subscription_id in subscriptions for u in data.usage]
    return links.count(False)


def _well_formed(case: object) -> bool:
    if not isinstance(case, dict):
        return False
    text = ("id", "why", "message", "customer_email")
    findings = case.get("expect_findings")
    return (
        all(isinstance(case.get(key), str) and case[key] for key in text)
        and (case.get("invoice_id") is None or isinstance(case.get("invoice_id"), str))
        and "invoice_id" in case
        and _one_of(case.get("expect_classification"), _CLASSIFICATIONS)
        and _one_of(case.get("expect_action"), ACTIONS)
        and isinstance(case.get("expect_human_approval"), bool)
        and isinstance(findings, list)
        and all(_one_of(f, _FINDINGS) for f in findings)
        and _one_of(case.get("expect_sender", SenderStatus.KNOWN.value), _SENDERS)
        and isinstance(case.get("expect_engine_skipped", False), bool)
        and isinstance(case.get("expect_automatic_reply", False), bool)
        and _headers(case.get("headers", {}))
    )


def _headers(headers: object) -> bool:
    """A name to a string, or to a list of strings (ADR-011)."""
    if not isinstance(headers, dict):
        return False
    for name, value in headers.items():
        values = value if isinstance(value, list) else [value]
        if not isinstance(name, str) or not all(isinstance(v, str) for v in values):
            return False
    return True


def _one_of(value: object, allowed: set[str] | frozenset[str]) -> bool:
    """A known string; a list or an object is malformed, not an exception."""
    return isinstance(value, str) and value in allowed


def _fits(case: dict, data: FixtureSet) -> bool:
    email = case["customer_email"].strip().lower()
    customer = next((c for c in data.customers if c.email.lower() == email), None)
    invoice = next((i for i in data.invoices if i.id == case["invoice_id"]), None)
    sender = case.get("expect_sender", SenderStatus.KNOWN.value)

    if sender in _UNKNOWN_SENDERS:
        if customer is not None:
            return False
        reaches = suspected_impersonation(
            case["customer_email"], case["message"], invoice, data.customers
        )
        return (reaches is not None) == (sender == SenderStatus.SUSPECTED_IMPERSONATION.value)

    if customer is None:
        return False
    if case["invoice_id"] is None:
        return True
    belongs = invoice is not None and invoice.customer_id == customer.id
    return belongs != (RuleCode.UNKNOWN_INVOICE.value in case["expect_findings"])

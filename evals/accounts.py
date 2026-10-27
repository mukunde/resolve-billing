"""Which vendor account a run uses, by a non-secret identity
(specs/007-openai-qwen-engines).

For the configurations of spec 004 the identity stays what it was: the first
12 hex characters of the key's SHA-256, a local credential fingerprint kept
in the private state, and the declared Gemini project. Their run lock is
named by vendor, or by the limits their account shares.

For a configuration on a prepaid account (spec 007), the identity is what
the maintainer declares in his `.env` -- the billing organisation and the
project -- never the key: rotating or adding a key changes nothing here, so
it never replenishes an envelope. The envelope and the run lock belong to
the billing organisation, which holds the prepaid balance; a first call's
acceptance to the project. These ids are not secrets; whether the vendor
checks them against the key is not documented (plan, research notes): they
are the maintainer's declaration.
"""

from __future__ import annotations

import hashlib
import os
import re

from app.decision.hosted import Configuration

#: Where a Gemini run names its project (`evals/hosted_run.py` reads it too).
QUOTA_PROJECT = "GEMINI_QUOTA_PROJECT"

#: A declared id also names files in the accounts' state: nothing else.
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class AccountRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The account is not named, or not in a form that can be used."""


def missing(config: Configuration) -> str | None:
    """The account variables not set, said; None when all are."""
    absent = [name for name in config.account_variables if not os.environ.get(name)]
    if absent:
        return f"{', '.join(absent)} not set: the {config.vendor} account is not named"
    return None


def declared(config: Configuration) -> dict[str, str]:
    """The account variables' values, or a refusal."""
    if (refusal := missing(config)) is not None:
        raise AccountRefused(refusal)
    values = {name: os.environ[name] for name in config.account_variables}
    for name, value in values.items():
        if not _ID.fullmatch(value):
            raise AccountRefused(f"{name} is not an account id (letters, digits, - and _ only)")
    return values


def identity(config: Configuration) -> dict:
    """The account a run uses, by a non-secret identity."""
    if config.account_variables:
        organisation, project = declared(config).values()
        return {"organisation": organisation, "project": project}
    key = os.environ.get(config.key_variable, "")
    legacy = {"key": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]}
    if config.requests_per_day is not None:
        legacy["project"] = os.environ.get(QUOTA_PROJECT, "")
    return legacy


def billing_account(config: Configuration) -> str:
    """The name of the account that holds the balance: its run lock and,
    for a prepaid one, its envelope."""
    if config.account_variables:
        organisation = next(iter(declared(config).values()))
        return f"{config.vendor}-{organisation}"
    return config.shared_limits or config.vendor

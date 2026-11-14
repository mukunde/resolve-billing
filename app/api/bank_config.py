"""Which bank, if any, the service reads for evidence that a payment arrived
(specs/016-bank-evidence). None until a bank is configured: the investigation then runs
exactly as before.

`BANK_EVIDENCE=revolut` reads Revolut Business's Sandbox, read-only, with:

- `REVOLUT_API_KEY`: a key of its own, Accounts and Transactions **Read** only -- never
  the Sandbox's pre-generated full-access key, which the seed alone uses;
- `REVOLUT_SIGNING_KEY_PATH`: the file holding that key's Ed25519 private signing key;
- `REVOLUT_SANDBOX_ACCOUNT_ID`: the declared account, the only one read;
- `REVOLUT_CONTROLLED_WORLDS=enabled`, by this exact word, for the seeded Sandbox run
  alone: the maintainer's declaration that every credit there was created inside the
  margin, without which coverage is never established (FR-003b).

The seed's key (`REVOLUT_SEED_KEY`) lives in `.env.seed`, which the service never loads:
finding it in the service's environment refuses the start. Any other value, or a missing
variable, fails visibly by its name, never with a value.
"""

from __future__ import annotations

import os

from app.providers.bank_base import BankEvidence

#: Banks that exist today.
BANKS = ("revolut",)


def bank_evidence() -> BankEvidence | None:
    selected = os.environ.get("BANK_EVIDENCE", "").strip().lower()
    if not selected:
        return None
    if selected != "revolut":
        raise RuntimeError(f"unknown bank evidence {selected!r}; available: {list(BANKS)}")
    return _revolut()


def _revolut() -> BankEvidence:
    from app.providers.revolut.adapter import RevolutBank
    from app.providers.revolut.client import (
        RevolutConfigurationError,
        RevolutReader,
        load_signing_key,
    )

    if os.environ.get("REVOLUT_SEED_KEY"):
        raise RevolutConfigurationError("seed_key_in_service_environment")
    values = {}
    for name in ("REVOLUT_API_KEY", "REVOLUT_SIGNING_KEY_PATH", "REVOLUT_SANDBOX_ACCOUNT_ID"):
        value = os.environ.get(name, "").strip()
        if not value:
            raise RevolutConfigurationError(f"missing {name}")
        values[name] = value
    reader = RevolutReader(
        values["REVOLUT_API_KEY"], load_signing_key(values["REVOLUT_SIGNING_KEY_PATH"])
    )
    return RevolutBank(
        reader,
        (values["REVOLUT_SANDBOX_ACCOUNT_ID"],),
        controlled=os.environ.get("REVOLUT_CONTROLLED_WORLDS") == "enabled",
    )

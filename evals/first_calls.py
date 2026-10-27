"""Accepted first calls, and the full runs they open
(specs/004-second-engines, second amendment).

A configuration's full run starts only after its first call
(`--first-call-only`) was accepted: a valid verdict, its usage reported and
within the reservation, and a model version returned. For a configuration
sharing its account's limits -- Mistral's two -- every configuration sharing
them must have been accepted too.

An acceptance is recorded in the accounts' state outside every checkout
(`evals/state.py`), after its result is written: the date, the model version
returned, the result's path and SHA-256, the account identity and the
configuration's fingerprint. A change to any of it -- a field, the prompt,
the schema, the masking, the estimator, the key or the Gemini project -- calls
for a new first call.

The account identity is never a secret: the first 12 hex characters of the
SHA-256 of the key, a local credential fingerprint that stays in this private
state, never in logs or published results; and the declared Gemini project.
That the key belongs to the account is the maintainer's declaration.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.decision.hosted import CONFIGURATIONS, Configuration
from app.decision.masking import MASKING_VERSION
from app.decision.verdict import verdict_schema
from evals import accounts, state


class FirstCallsRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """A full run is not admitted, or the record cannot be used as it stands."""


def fingerprint(config: Configuration) -> str:
    """SHA-256 of everything that shapes a request and its reading."""
    whole = {
        "configuration": _shaping(config),
        "prompt_version": config.prompt_version,
        "schema": verdict_schema(),
        "masking_version": MASKING_VERSION,
        "estimator": config.reservation_estimator,
    }
    canonical = json.dumps(whole, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: The fields added after spec 004, with the value the spec 004
#: configurations hold. Fixed here, not read from the dataclass: a later
#: change of a default must move a fingerprint, never pass unseen.
ADDED_SINCE_SPEC_004 = {
    "currency": None,
    "cached_input_price_per_mtok": None,
    "cache_write_price_per_mtok": None,
    "processing_location": None,
    "quantisation": None,
    "reasoning_note": None,
    "account_variables": [],
    "store": None,
    "check_identity": False,
}


def _shaping(config: Configuration) -> dict:
    """The configuration's fields, leaving out a field added after spec 004
    while it holds the value spec 004's configurations have: those four
    keep the fingerprint their first calls were accepted on."""
    return {
        name: value
        for name, value in asdict(config).items()
        if name not in ADDED_SINCE_SPEC_004 or _listed(value) != ADDED_SINCE_SPEC_004[name]
    }


def _listed(value):
    return list(value) if isinstance(value, tuple) else value


def account(config: Configuration) -> dict:
    """The account a run uses, by a non-secret identity (`evals/accounts.py`):
    spec 004's key fingerprint and Gemini project; for a prepaid account,
    its declared organisation and project, never the key."""
    return accounts.identity(config)


class FirstCalls:
    def __init__(self, lock_timeout: float = 30.0) -> None:
        self._lock_timeout = lock_timeout

    @property
    def path(self) -> Path:
        return state.STATE_DIR / "first-calls.json"

    def record(self, config: Configuration, model_version: str, result: Path) -> None:
        """An accepted first call, recorded once its result is on disk."""
        with self._locked():
            held = self._read()
            held["first_calls"][config.name] = {
                "accepted_at": datetime.now(UTC).isoformat(),
                "model_version": model_version,
                "result": str(Path(result).resolve()),
                "result_sha256": hashlib.sha256(Path(result).read_bytes()).hexdigest(),
                "account": account(config),
                "fingerprint": fingerprint(config),
            }
            state.write_atomically(self.path, held)

    def admit_full_run(self, config: Configuration) -> None:
        """Refused unless this configuration -- and every one sharing its
        account's limits -- has an accepted first call matching it now."""
        required = [config]
        if config.shared_limits is not None:
            required += [
                other
                for other in CONFIGURATIONS.values()
                if other.shared_limits == config.shared_limits and other.name != config.name
            ]
        with self._locked():
            held = self._read()
        missing = []
        for one in required:
            record = held["first_calls"].get(one.name)
            if record is None:
                missing.append(f"{one.name} (no accepted first call)")
            elif record["fingerprint"] != fingerprint(one):
                missing.append(f"{one.name} (changed since its first call)")
            elif record["account"] != account(one):
                missing.append(f"{one.name} (first call on another key or project)")
            elif not _evidence_holds(record):
                missing.append(f"{one.name} (its first call's result is missing or changed)")
        if missing:
            raise FirstCallsRefused(
                "a full run needs an accepted first call (--first-call-only) for: "
                + ", ".join(missing)
            )

    def _read(self) -> dict:
        if not self.path.exists():
            return {"first_calls": {}}
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FirstCallsRefused(
                f"the first calls' record {self.path} cannot be read ({type(error).__name__})"
            ) from None
        if not _consistent(held):
            raise FirstCallsRefused(
                f"the first calls' record {self.path} is inconsistent; it is never reset"
            )
        return held

    @contextmanager
    def _locked(self) -> Iterator[None]:
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(state.STATE_DIR / "first-calls.lock", "a+b") as lock:
            deadline = time.monotonic() + self._lock_timeout
            while not state.try_lock(lock):
                if time.monotonic() > deadline:
                    raise FirstCallsRefused("the first calls' record is held by another run")
                time.sleep(0.02)
            try:
                yield
            finally:
                state.unlock(lock)


def _evidence_holds(record: dict) -> bool:
    """The first call's result still where it was written, unchanged."""
    path = Path(record["result"])
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == record["result_sha256"]
    except OSError:
        return False


_FIELDS = {"accepted_at", "model_version", "result", "result_sha256", "account", "fingerprint"}


def _consistent(held) -> bool:
    if not isinstance(held, dict) or set(held) != {"first_calls"}:
        return False
    if not isinstance(held["first_calls"], dict):
        return False
    for record in held["first_calls"].values():
        if not isinstance(record, dict) or set(record) != _FIELDS:
            return False
        if not isinstance(record["account"], dict):
            return False
        if not all(
            isinstance(record[field], str) and record[field] for field in _FIELDS - {"account"}
        ):
            return False
    return True

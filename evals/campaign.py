"""The comparison's campaign: which run fills which slot, in what order
(specs/008-deployment-model-choice).

The comparison that chooses the deployment model counts only runs declared
in a manifest before any of them is made. Each counted run is launched
with `--slot`; nothing is inferred afterwards. A slot is admitted only when
its turn has come: rounds in the manifest's order, each slot's one
permitted replacement taken next, a candidate that stops completing retired
from its remaining slots. Admission takes one lock for the whole campaign,
whatever the vendor, and writes a line to the campaign log -- flushed and
synced before the run sends anything. The log lives with the results and
is committed with them, so the comparison is reproducible from the
repository alone.

A run records each case as it is decided, in a per-attempt file beside the
results. If its process dies before writing its result, the next admission
rebuilds that result from those cases -- its unsafe detections counted --
and the slot may take its replacement. If those cases cannot be read, the
attempt is lost and the candidate stops completing: an attempt whose
evidence is gone is never replaced.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.decision.claude import PROMPT_VERSION
from app.decision.hosted import PROVISIONAL_LIMITS, configuration
from app.decision.masking import MASKING_VERSION
from app.decision.verdict import verdict_schema
from evals import engines, state
from evals.first_calls import fingerprint

REPO = Path(__file__).resolve().parents[1]
#: The comparison's name, its manifest, where its runs and its log are kept.
NAME = "008"
MANIFEST = REPO / "evals" / "comparisons" / NAME / "manifest.json"
RESULTS_DIR = REPO / "evals" / "results"
LOG_NAME = f"campaign-{NAME}.jsonl"
CASES_DIR = f"campaign-{NAME}-cases"

#: Stops that come from outside the model's answers: one replacement each.
#: The prefixes are this code's own words (`evals/governed.py`,
#: `evals/hosted_run.py`), never a vendor's.
QUALIFYING = (
    "interrupted",
    "the run's deadline",
    "rate limiting persisted",
    "outage:",
    "the quota refused",
    "process lost",
)

#: What a recorded case's outcome must hold: the runner's scored checks,
#: the unsafe flag first among them.
OUTCOME_FIELDS = frozenset(
    {
        "case_id",
        "classification_ok",
        "action_ok",
        "approval_ok",
        "findings_ok",
        "skip_ok",
        "unsafe",
        "sender_ok",
        "reply_ok",
    }
)

#: The statuses a recorded case may carry.
STATUSES = frozenset(
    {"decided", "abstained", "invalid", "unavailable", "not_sent_oversized", "rules_only"}
)

REBUILT_REASON = "process lost; rebuilt from its per-case record"


class CampaignRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """A slot is not admitted, or the campaign cannot be read as it stands."""


def stop_kind(reason: str | None) -> str | None:
    """`qualifying` for a stop outside the model's answers, `other` for any
    other stop, None when the run did not stop."""
    if reason is None:
        return None
    return "qualifying" if reason.startswith(QUALIFYING) else "other"


def current_evidence() -> dict:
    """What every counted result must carry, as the code stands."""
    return {
        "cases_sha256": engines.DEVELOPMENT_SET_SHA256[:16],
        "prompt_version": PROMPT_VERSION,
        "schema_sha256": hashlib.sha256(
            json.dumps(verdict_schema(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "masking_version": MASKING_VERSION,
    }


def clean_from_porcelain(lines: list[str]) -> bool:
    """Clean: nothing modified or staged, nothing untracked but results."""
    for line in lines:
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if not (line.startswith("?? ") and path.startswith("evals/results/")):
            return False
    return True


def git_state(root: Path = REPO) -> dict:
    """The commit and whether the tree is clean, asked of the checkout this
    code lives in, whatever the working directory."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "clean_tree": "unknown"}
    return {"commit": commit, "clean_tree": clean_from_porcelain(status)}


def manifest_commit(path: Path, root: Path = REPO) -> str | None:
    """The last commit that touched the manifest: the commit the runs execute
    on. None outside git, or for a manifest never committed."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H", "--", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit or None


def load_manifest(path: Path) -> dict:
    try:
        held = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CampaignRefused(
            f"the manifest {path} cannot be read ({type(error).__name__})"
        ) from None
    required = {"comparison", "candidates", "comparison_points", "evidence", "rounds"}
    if not isinstance(held, dict) or not required <= set(held) or held["comparison"] != NAME:
        raise CampaignRefused(f"the manifest {path} is not comparison {NAME}'s")
    return held


def _ledger_account(config) -> str | None:
    """The ledger a configuration's attempts are reserved in, by account."""
    from evals import accounts

    if config.vendor == "mistral":
        return "mistral"
    if config.currency is not None:
        return accounts.billing_account(config)
    return None


def slot_id(round_index: int, name: str) -> str:
    return f"r{round_index + 1}-{name}"


@dataclass
class CampaignState:
    #: Per slot, in the manifest's order: open, unresolved, complete,
    #: pending_replacement, failed or retired.
    slots: dict[str, str] = field(default_factory=dict)
    #: The configuration each slot runs.
    engines: dict[str, str] = field(default_factory=dict)
    not_completing: set[str] = field(default_factory=set)

    def next_slot(self) -> str | None:
        for slot, status in self.slots.items():
            # A failed slot is over too: its candidate stops completing.
            if status not in {"complete", "retired", "failed"}:
                return slot
        return None


class Campaign:
    def __init__(
        self,
        manifest_path: Path | None = None,
        results_dir: Path | None = None,
        state_dir: Path | None = None,
    ) -> None:
        # Read at call time, so a test can point them elsewhere.
        self.manifest_path = Path(manifest_path or MANIFEST)
        self.manifest = load_manifest(self.manifest_path)
        self.results_dir = Path(results_dir or RESULTS_DIR)
        self._state_dir = state_dir

    # -- reading ---------------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.results_dir / LOG_NAME

    def log_lines(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        try:
            return [
                json.loads(line)
                for line in self.log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except ValueError:
            raise CampaignRefused(f"the campaign log {self.log_path} cannot be read") from None

    def results(self) -> dict[tuple[str, int], dict]:
        """Every counted result in the results folder, by slot and attempt."""
        found: dict[tuple[str, int], dict] = {}
        for path in sorted(self.results_dir.glob("*.json")):
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(held, dict) and held.get("campaign") == NAME:
                key = (held.get("slot"), held.get("attempt"))
                if key in found:
                    raise CampaignRefused(f"two results fill slot {key[0]}, attempt {key[1]}")
                found[key] = held | {"_file": path.name}
        return found

    def declared_slots(self) -> dict[str, str]:
        return {
            slot_id(index, name): name
            for index, names in enumerate(self.manifest["rounds"])
            for name in names
        }

    def state(self) -> CampaignState:
        lines = self.log_lines()
        results = self.results()
        attempts: dict[str, list[int]] = {}
        lost = set()
        retired = set()
        for line in lines:
            kind = line.get("type")
            if kind == "attempt":
                attempts.setdefault(line["slot"], []).append(line["attempt"])
            elif kind == "lost":
                lost.add((line["slot"], line["attempt"]))
            elif kind == "retired":
                retired.add(line["slot"])
        held = CampaignState(engines=self.declared_slots())
        for slot in held.engines:
            held.slots[slot] = self._status(slot, attempts.get(slot, []), results, lost, retired)
            if held.slots[slot] == "failed":
                held.not_completing.add(held.engines[slot])
        return held

    @staticmethod
    def _status(slot, attempts, results, lost, retired) -> str:
        if slot in retired:
            return "retired"
        status = "open"
        for attempt in attempts:
            if (slot, attempt) in lost:
                return "failed"
            outcome = results.get((slot, attempt))
            if outcome is None:
                return "unresolved"
            if outcome.get("complete"):
                return "complete"
            if attempt >= 2 or stop_kind(outcome.get("stop_reason")) != "qualifying":
                return "failed"
            status = "pending_replacement"
        return status

    # -- admission -------------------------------------------------------

    @contextmanager
    def admit(self, engine_name: str, slot: str, lock_timeout: float = 30.0, run_id: str = ""):
        """The slot, admitted and logged, held for the run; or a refusal."""
        declared = self.declared_slots()
        if slot not in declared:
            raise CampaignRefused(f"slot {slot} is not declared in the manifest")
        if declared[slot] != engine_name:
            raise CampaignRefused(
                f"slot {slot} runs another configuration ({declared[slot]}), not {engine_name}"
            )
        config = configuration(engine_name)
        entry = self.manifest["candidates"].get(engine_name) or self.manifest[
            "comparison_points"
        ].get(engine_name)
        if entry is None or entry.get("fingerprint") != fingerprint(config):
            raise CampaignRefused(
                f"{engine_name}'s configuration fingerprint is not the manifest's"
            )
        if config.limits_source == PROVISIONAL_LIMITS:
            raise CampaignRefused(f"{engine_name}'s limits are provisional; read them first")
        repo = git_state()
        if repo["clean_tree"] is not True:
            raise CampaignRefused("the source tree is not clean: commit or remove the changes")
        with self._locked(lock_timeout):
            if self.manifest.get("evidence") != current_evidence():
                raise CampaignRefused(
                    "the code's evidence (cases, prompt, schema, masking) is not the manifest's"
                )
            if repo["commit"] != manifest_commit(self.manifest_path):
                raise CampaignRefused(
                    "the checkout is not at the approved manifest revision's commit"
                )
            self._bind(repo["commit"])
            self._recover()
            self._retire()
            held = self.state()
            if declared[slot] in held.not_completing:
                raise CampaignRefused(f"{engine_name} is not completing: its slots are retired")
            nxt = held.next_slot()
            if slot != nxt:
                raise CampaignRefused(f"slot {slot} is not the next slot ({nxt})")
            attempt = 2 if held.slots[slot] == "pending_replacement" else 1
            self._append(
                {
                    "type": "attempt",
                    "slot": slot,
                    "attempt": attempt,
                    "engine": engine_name,
                    "started_at": datetime.now(UTC).isoformat(),
                    "commit": repo["commit"],
                }
            )
            admitted = Admission(self, slot, attempt, engine_name)
            # What the run is, fixed at its start, for a recovery to rebuild
            # a result as it would have been written.
            admitted.write_meta(
                {
                    "engine": engine_name,
                    "commit": repo["commit"],
                    "configuration": asdict(config),
                    "configuration_fingerprint": fingerprint(config),
                    # The prefix of the run's tickets in its account's ledger,
                    # and that ledger's account, as they were at the start.
                    "run_id": run_id,
                    "ledger_account": _ledger_account(config),
                }
                | current_evidence()
            )
            try:
                yield admitted
            finally:
                admitted.close()

    def _recover(self) -> None:
        """Rebuild the result of a run that died before writing it, or record
        it lost when its cases cannot be read."""
        results = self.results()
        lost = {
            (line["slot"], line["attempt"])
            for line in self.log_lines()
            if line.get("type") == "lost"
        }
        for line in self.log_lines():
            if line.get("type") != "attempt":
                continue
            key = (line["slot"], line["attempt"])
            if key in results or key in lost:
                continue
            try:
                meta, recorded = self._read_record(*key)
            except (OSError, ValueError, KeyError, TypeError):
                self._append({"type": "lost", "slot": key[0], "attempt": key[1]})
                continue
            last = recorded[-1]["accounting"] if recorded else {}
            # As the runner decides: a case too large to send would be too
            # large again, so it is never a replaceable loss.
            oversized = sum(1 for r in recorded if r["status"] == "not_sent_oversized")
            reason = (
                f"not sent: {oversized} cases too large for the quota (then: {REBUILT_REASON})"
                if oversized
                else REBUILT_REASON
            )
            # What the account's ledger holds for this run -- the attempt in
            # flight when the process died included -- overrides the record.
            ledgered = self._ledgered(meta)
            if ledgered is not None:
                last = last | ledgered
            rebuilt = {
                "campaign": NAME,
                "slot": key[0],
                "attempt": key[1],
                "engine": meta["engine"],
                "rebuilt": True,
                "complete": False,
                "stop_reason": reason,
                "stop_kind": stop_kind(reason),
                "commit": meta["commit"],
                "clean_tree": True,
                "configuration": meta["configuration"],
                "configuration_fingerprint": meta["configuration_fingerprint"],
                "outcomes": [r["outcome"] for r in recorded],
                "statuses": {r["case_id"]: r["status"] for r in recorded},
                "usage": {r["case_id"]: r["accounting"].get("usage") for r in recorded},
                "sends": last.get("sends", 0),
                "cost": {
                    "settled_amount": last.get("cost", 0.0),
                    "currency": meta["configuration"].get("currency"),
                    "unknown_cost_attempts": last.get("unknown_cost_attempts", 0),
                    "unknown_cost_reserved": last.get("unknown_cost_reserved", 0.0),
                    "from_ledger": ledgered is not None and "ledger_unavailable" not in ledgered,
                    "ledger_unavailable": bool(ledgered and ledgered.get("ledger_unavailable")),
                    "complete": False,
                },
            } | {k: meta[k] for k in current_evidence()}
            target = self.results_dir / f"{meta['engine']}-{key[0]}-a{key[1]}-rebuilt.json"
            state.write_atomically(target, rebuilt)

    @staticmethod
    def _ledgered(meta: dict) -> dict | None:
        """The run's settled cost and its unsettled reservations, read from
        its account's ledger by the run's ticket prefix; None when the
        configuration keeps no ledger or it cannot be read."""
        from evals.ledger import Ledger, LedgerRefused, PrepaidLedger

        account = meta.get("ledger_account")
        if account is None:
            # A configuration that keeps no ledger (a free tier).
            return None
        try:
            if account == "mistral":
                book = Ledger()
            else:
                book = PrepaidLedger(account, meta["configuration"]["currency"])
            held = book._read()
        except (LedgerRefused, KeyError, TypeError):
            held = None
        if held is None or not meta.get("run_id"):
            # The run's own ledger cannot be read: its accounting is unknown,
            # never taken from the stale case record as if complete.
            return {"ledger_unavailable": True}
        mine = [
            entry
            for ticket, entry in held["attempts"].items()
            if ticket.startswith(f"{meta['run_id']}/")
        ]
        open_ = [entry["reserved"] for entry in mine if entry["settled"] is None]
        return {
            "sends": len(mine),
            "cost": sum(entry["settled"] for entry in mine if entry["settled"] is not None),
            "unknown_cost_attempts": len(open_),
            "unknown_cost_reserved": sum(open_),
        }

    def _read_record(self, slot: str, attempt: int) -> tuple[dict, list[dict]]:
        """The run's metadata and its recorded cases, each checked; any flaw
        raises, and the attempt is then lost."""
        meta = json.loads(self.meta_path(slot, attempt).read_text(encoding="utf-8"))
        strings = ("engine", "commit", "configuration_fingerprint", *current_evidence())
        if not (
            isinstance(meta, dict)
            and all(isinstance(meta.get(key), str) and meta[key] for key in strings)
            and isinstance(meta.get("configuration"), dict)
            and meta["configuration"].get("name") == meta["engine"]
        ):
            raise ValueError("incomplete metadata")
        rows = self.cases_path(slot, attempt).read_text(encoding="utf-8").splitlines()
        recorded = [json.loads(row) for row in rows if row.strip()]
        known = {case["id"] for case in json.loads(engines.DEVELOPMENT_SET.read_bytes())}
        seen = set()
        for r in recorded:
            if not (
                isinstance(r, dict)
                and set(r) == {"case_id", "outcome", "status", "accounting"}
                and r["case_id"] in known
                and r["case_id"] not in seen
                and isinstance(r["outcome"], dict)
                and set(r["outcome"]) >= OUTCOME_FIELDS
                and r["outcome"]["case_id"] == r["case_id"]
                # `CaseOutcome.unsafe`: None, or the reason it was unsafe.
                and (
                    r["outcome"]["unsafe"] is None
                    or (isinstance(r["outcome"]["unsafe"], str) and r["outcome"]["unsafe"])
                )
                and r["status"] in STATUSES
                and isinstance(r["accounting"], dict)
            ):
                raise ValueError("a malformed case record")
            seen.add(r["case_id"])
        return meta, recorded

    def _bind(self, commit: str) -> None:
        """One campaign, one history: the first admission binds the campaign
        to its results folder in the private state; another folder -- another
        worktree's, say -- would start an empty history, and is refused."""
        directory = self._state_dir or state.STATE_DIR
        path = directory / f"campaign-{NAME}-binding.json"
        here = {"manifest_commit": commit, "results_dir": str(self.results_dir.resolve())}
        if not path.exists():
            state.write_atomically(path, here)
            return
        try:
            bound = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CampaignRefused(f"the campaign's binding {path} cannot be read") from None
        if bound != here:
            raise CampaignRefused(
                f"the campaign is bound to {bound.get('results_dir')} at "
                f"{bound.get('manifest_commit')}; another folder or revision is refused"
            )

    def _retire(self) -> None:
        held = self.state()
        for slot, name in held.engines.items():
            if name in held.not_completing and held.slots[slot] == "open":
                self._append({"type": "retired", "slot": slot})

    def _append(self, line: dict) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(line) + "\n")
            log.flush()
            os.fsync(log.fileno())

    def cases_path(self, slot: str, attempt: int) -> Path:
        return self.results_dir / CASES_DIR / f"{slot}-a{attempt}.jsonl"

    def meta_path(self, slot: str, attempt: int) -> Path:
        return self.results_dir / CASES_DIR / f"{slot}-a{attempt}.meta.json"

    @contextmanager
    def _locked(self, timeout: float):
        directory = self._state_dir or state.STATE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"campaign-{NAME}.lock", "a+b") as lock:
            deadline = time.monotonic() + timeout
            while not state.try_lock(lock):
                if time.monotonic() > deadline:
                    raise CampaignRefused("another run of the campaign holds its lock")
                time.sleep(0.05)
            try:
                yield
            finally:
                state.unlock(lock)


class Admission:
    """A slot held for one run."""

    def __init__(self, campaign: Campaign, slot: str, attempt: int, engine: str) -> None:
        self.campaign = campaign
        self.slot = slot
        self.attempt = attempt
        self.engine = engine

    def write_meta(self, meta: dict) -> None:
        path = self.campaign.meta_path(self.slot, self.attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        state.write_atomically(path, meta)

    def record_case(
        self, case_id: str, outcome: dict, status: str, accounting: dict | None = None
    ) -> None:
        """One decided case, with the run's accounting so far, on disk before
        the run goes on."""
        path = self.campaign.cases_path(self.slot, self.attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "case_id": case_id,
            "outcome": outcome,
            "status": status,
            "accounting": accounting or {},
        }
        with open(path, "a", encoding="utf-8") as cases:
            cases.write(json.dumps(row) + "\n")
            cases.flush()
            os.fsync(cases.fileno())

    def close(self) -> None:
        """Once the run's result is written, its per-case record is no longer
        needed; without a result it stays, for recovery."""
        if (self.slot, self.attempt) in self.campaign.results():
            self.campaign.cases_path(self.slot, self.attempt).unlink(missing_ok=True)
            self.campaign.meta_path(self.slot, self.attempt).unlink(missing_ok=True)

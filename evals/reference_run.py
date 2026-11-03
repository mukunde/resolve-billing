"""The holdout's reference run, once, on the adopted composite
(specs/011-reference-run).

Run by the maintainer alone, never by an agent on the real holdout. Three
commands:

    python -m dotenv run -- python -m evals.reference_run prepare \\
        --cases PATH --fixtures DIR --private DIR [--threshold N]
    python -m dotenv run -- python -m evals.reference_run run --freeze HASH --private DIR
    python -m evals.reference_run rebuild --private DIR

`prepare` copies every input into the private folder once and writes the
freeze record there, sending nothing. `run`, after the maintainer types
RUN ONCE, spends the one authorisation before its first send and runs the
snapshot through `gpt-6-luna+claude-haiku-5-5`, fallback on. `rebuild`
recomputes the totals from the records, calling nothing.

Constitution, principle IX: a session that tunes the system does not read the
cases. So nothing case-level leaves the private folder: before anything else,
standard output, standard error, their file descriptors and every logger go to
the folder's log, and only an allowlist -- the summary, the prompt, the totals,
a refusal's category -- reaches the console. Only the standard library is
imported before that.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
#: The one reference run: this holdout's, frozen on 2026-10-22. It does not
#: depend on the inputs, so no other folder, freeze record or case file opens
#: a second run.
IDENTITY = "holdout-2026-10-22"
#: The merge that made an uncertain check send a case to a person
#: (`.specify/bugs/uncertain-human-review/`): the run's commit must contain it.
UNCERTAINTY_FIX = "83ac248"
#: Where the one-shot state lives: outside the repository and every checkout.
STATE_DIR = Path.home() / ".resolve-billing" / "reference"
CONFIRMATION = "RUN ONCE"
FIXTURE_NAMES = (
    "customers.json",
    "subscriptions.json",
    "invoices.json",
    "payments.json",
    "usage.json",
)


class Refused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The run may not go on. Its text is a category, never data."""


# -- lockdown ------------------------------------------------------------------


class Lockdown:
    """Every output into the private log, but an allowlisted console."""

    def __init__(self, private: Path, descriptors: bool = True) -> None:
        self.log_path = private / "log.txt"
        self._descriptors = descriptors
        self._saved: list = []
        self.console = None

    def __enter__(self) -> Lockdown:
        self._log = open(self.log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout.flush()
        sys.stderr.flush()
        if self._descriptors:
            # The console first, on a copy of the real descriptor, before
            # descriptor 1 is pointed at the log.
            self.console = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
            self._saved = [os.dup(1), os.dup(2)]
            os.dup2(self._log.fileno(), 1)
            os.dup2(self._log.fileno(), 2)
        else:
            self.console = sys.__stdout__
        self._streams = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = self._log
        root = logging.getLogger()
        self._root = (list(root.handlers), root.level)
        root.handlers = [logging.StreamHandler(self._log)]
        root.setLevel(logging.INFO)
        self._named = []
        for logger in list(logging.root.manager.loggerDict.values()):
            if isinstance(logger, logging.Logger):
                self._named.append((logger, list(logger.handlers), logger.propagate))
                logger.handlers.clear()
                logger.propagate = True
        return self

    def say(self, line: str) -> None:
        self.console.write(line + "\n")
        self.console.flush()

    def __exit__(self, *exc) -> None:
        root = logging.getLogger()
        root.handlers, level = self._root
        root.setLevel(level)
        for logger, handlers, propagate in self._named:
            logger.handlers[:] = handlers
            logger.propagate = propagate
        sys.stdout, sys.stderr = self._streams
        if self._descriptors:
            os.dup2(self._saved[0], 1)
            os.dup2(self._saved[1], 2)
            for saved in self._saved:
                os.close(saved)
            self.console.close()
        self._log.close()


def refuse_tracing() -> None:
    """No span may leave: an exporter configured, or a provider installed."""
    if any(
        key.startswith("OTEL_") and ("EXPORTER" in key or "ENDPOINT" in key) for key in os.environ
    ):
        raise Refused("a trace exporter is configured: unset every OTEL_ exporter variable")
    if tracer_installed():
        raise Refused("a tracer provider is installed")


def tracer_installed() -> bool:
    """Whether a provider other than the default no-op one is installed."""
    if "opentelemetry.trace" not in sys.modules:
        return False
    from opentelemetry import trace

    return type(trace.get_tracer_provider()).__name__ not in {
        "ProxyTracerProvider",
        "NoOpTracerProvider",
    }


def private_folder(raw: str) -> Path:
    """The maintainer's folder, outside every repository and worktree."""
    try:
        folder = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise Refused("the private folder does not exist") from None
    if not folder.is_dir():
        raise Refused("the private folder is not a folder")
    probe = folder / ".write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        raise Refused("the private folder is not writable") from None
    for parent in (folder, *folder.parents):
        if (parent / ".git").exists():
            raise Refused("the private folder is inside a repository")
    for tree in _worktrees():
        if folder == tree or tree in folder.parents:
            raise Refused("the private folder is inside a worktree")
    return folder


def _worktrees() -> list[Path]:
    listed = subprocess.run(
        ["git", "-C", str(REPOSITORY), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    trees = [REPOSITORY]
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            with contextlib.suppress(OSError, RuntimeError):
                trees.append(Path(line[len("worktree ") :]).resolve())
    return trees


# -- the record files ----------------------------------------------------------


def write_whole(path: Path, content: dict) -> None:
    """A whole file or none: written aside, synced, renamed, folder synced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(content, out, ensure_ascii=False, indent=1, default=str)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _read_all(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
        if not path.name.startswith(".")
    ]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- the one-shot state ---------------------------------------------------------


def state_path() -> Path:
    return STATE_DIR / f"{IDENTITY}.json"


@contextlib.contextmanager
def state_lock():
    from evals.state import try_lock, unlock

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(STATE_DIR / f"{IDENTITY}.lock", "a+b")  # noqa: SIM115
    if not try_lock(handle):
        handle.close()
        raise Refused("another reference run holds the state")
    try:
        yield
    finally:
        unlock(handle)
        handle.close()


def read_state() -> dict | None:
    path = state_path()
    if not path.exists():
        return None
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("the state is unreadable: it counts as consumed") from None
    if not isinstance(held, dict) or held.get("identity") != IDENTITY:
        raise Refused("the state is unreadable: it counts as consumed")
    return held


def write_state(content: dict) -> None:
    write_whole(state_path(), content)


# -- prepare -------------------------------------------------------------------


def snapshot(cases: Path, fixtures: Path, private: Path) -> None:
    """Copy every input once: from here on only the copy is read."""
    from app.providers.fake.fixtures import FIXTURES_DIR

    target = private / "snapshot"
    if target.exists():
        import shutil

        shutil.rmtree(target)
    (target / "base").mkdir(parents=True)
    (target / "extra").mkdir()
    (target / "cases.json").write_bytes(cases.read_bytes())
    for name in FIXTURE_NAMES:
        (target / "base" / name).write_bytes((FIXTURES_DIR / name).read_bytes())
    for path in sorted(fixtures.iterdir()):
        if path.is_file():
            (target / "extra" / path.name).write_bytes(path.read_bytes())


def scoring_definition() -> str:
    import inspect

    from evals import runner

    parts = [runner._evaluate, runner.unsafe_reason, runner._unsafe_reason]
    return _sha256("".join(inspect.getsource(part) for part in parts).encode("utf-8"))


def repository_state() -> dict:
    from evals import campaign

    repo = campaign.git_state()
    contains = subprocess.run(
        ["git", "-C", str(REPOSITORY), "merge-base", "--is-ancestor", UNCERTAINTY_FIX, "HEAD"],
        capture_output=True,
    )
    return {
        "commit": repo["commit"],
        "clean_tree": repo["clean_tree"],
        "uncertainty_fix": contains.returncode == 0,
    }


def freeze_record(
    private: Path, threshold: float | None = None, budget: dict | None = None
) -> dict:
    """What the run is bound to, read from the snapshot."""
    from app.api.deps import FAILOVER, HAIKU, LUNA
    from app.decision.failover import DEADLINE, POLICY, PRIMARY_BOUNDS, SECONDARY_BOUNDS
    from app.decision.hosted import PROMPT_VERSION, configuration
    from app.decision.masking import MASKING_VERSION
    from app.decision.verdict import verdict_schema
    from evals import runner
    from evals.first_calls import fingerprint

    target = private / "snapshot"
    cases = json.loads((target / "cases.json").read_text(encoding="utf-8"))
    files = {
        str(path.relative_to(target)).replace("\\", "/"): _sha256(path.read_bytes())
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }
    from dataclasses import asdict

    return {
        "identity": IDENTITY,
        **repository_state(),
        "configurations": {name: fingerprint(configuration(name)) for name in (LUNA, HAIKU)},
        "composite": {
            "name": FAILOVER,
            "policy": POLICY,
            "deadline_s": DEADLINE,
            "bounds": [asdict(PRIMARY_BOUNDS), asdict(SECONDARY_BOUNDS)],
        },
        "prompt_version": PROMPT_VERSION,
        "schema_sha256": _sha256(json.dumps(verdict_schema(), sort_keys=True).encode()),
        "masking_version": MASKING_VERSION,
        "case_sha256": files["cases.json"],
        "files": files,
        "case_count": len(cases),
        "evaluation_date": runner.EVALUATION_DATE.isoformat(),
        "scoring_sha256": scoring_definition(),
        "threshold": threshold,
        "budget": budget,
    }


def freeze_hash(record: dict) -> str:
    return _sha256(json.dumps(record, sort_keys=True).encode("utf-8"))


class DryEngine:
    """Both legs' requests built and reserved, nothing sent: the budget."""

    name = "dry"
    version = "dry"

    def __init__(self, legs) -> None:
        self._legs = legs
        self.reserved: dict[str, float] = {}
        self._finder = None

    @property
    def invoice_references(self):
        return self._finder

    @invoice_references.setter
    def invoice_references(self, finder) -> None:
        self._finder = finder
        for engine in self._legs:
            engine.invoice_references = finder

    async def decide(self, dispute, context, findings):
        from app.decision.base import EngineTelemetry
        from app.decision.verdict import escalation
        from evals import accounts
        from evals.governed import reservation_cost

        for engine in self._legs:
            system, user, _ = engine.request_texts(dispute, context, findings)
            tokens = await engine.reservation(system, user)
            account = accounts.billing_account(engine.config)
            amount = reservation_cost(tokens, engine.config)
            self.reserved[account] = self.reserved.get(account, 0.0) + amount
        return escalation(EngineTelemetry(model_id="dry", prompt_version="dry"))


def _cases_and_provider(private: Path):
    from app.providers.fake.adapter import FakeBillingProvider
    from app.providers.fake.fixtures import load_from

    target = private / "snapshot"
    cases = json.loads((target / "cases.json").read_text(encoding="utf-8"))
    provider = FakeBillingProvider(fixtures=load_from(target / "base", target / "extra"))
    return cases, provider


def _dispute(case: dict):
    from app.domain.models import Dispute
    from evals import runner

    return Dispute(
        id=f"reference_{case['id']}",
        customer_message=case["message"],
        customer_email=case["customer_email"],
        customer_id=case.get("customer_id"),
        invoice_id=case["invoice_id"],
        headers=runner.header_pairs(case.get("headers")),
    )


async def estimate(private: Path, legs) -> dict[str, float]:
    from app.api.deps import wire
    from app.application.investigate_dispute import DisputeInvestigator
    from evals import runner

    cases, provider = _cases_and_provider(private)
    dry = DryEngine(legs)
    investigator = DisputeInvestigator(provider=provider, engine=wire(dry, provider))
    for case in cases:
        await investigator.investigate(_dispute(case), now=runner.EVALUATION_DATE)
    return {account: round(amount, 6) for account, amount in dry.reserved.items()}


def legs_for_budget():
    from app.decision.hosted import configuration
    from app.decision.openai_compatible import OpenAICompatibleEngine
    from app.decision.vendors import client_for

    return [
        OpenAICompatibleEngine(configuration("gpt-6-luna")),
        client_for(configuration("claude-haiku-5-5")),
    ]


def admissible(budget: dict[str, float]) -> None:
    """Both accounts declared, both first calls accepted, both envelopes room."""
    from app.decision.hosted import configuration
    from evals import accounts
    from evals.first_calls import FirstCalls, FirstCallsRefused
    from evals.ledger import LedgerRefused, PrepaidLedger

    for name in ("gpt-6-luna", "claude-haiku-5-5"):
        config = configuration(name)
        try:
            accounts.declared(config)
            FirstCalls().admit_full_run(config)
            left = PrepaidLedger(accounts.billing_account(config), config.currency).state()["left"]
        except (accounts.AccountRefused, FirstCallsRefused, LedgerRefused) as refusal:
            raise Refused(f"{name}: {type(refusal).__name__}") from None
        if left < budget.get(accounts.billing_account(config), 0.0):
            raise Refused(f"{name}: the envelope cannot admit the estimated budget")


def prepare(args, say, legs=None) -> int:
    import asyncio

    from evals.check import check

    private = Path(args.private)
    snapshot(Path(args.cases), Path(args.fixtures), private)
    target = private / "snapshot"
    if check(target / "cases.json", fixtures=(target / "base", target / "extra")) != 0:
        raise Refused("the check failed")
    state = repository_state()
    if state["clean_tree"] is not True:
        raise Refused("the source tree is not clean")
    if not state["uncertainty_fix"]:
        raise Refused("the commit does not contain the uncertainty fix")
    budget = asyncio.run(estimate(private, legs if legs is not None else legs_for_budget()))
    admissible(budget)
    record = freeze_record(private, args.threshold, budget)
    digest = freeze_hash(record)
    with state_lock():
        held = read_state()
        if held is not None and held.get("state") != "prepared":
            raise Refused("the reference run was already consumed")
        if held is not None and held.get("freeze_hash") != digest:
            raise Refused("a reference run was prepared on other inputs")
        write_whole(private / "freeze.json", record)
        write_state(
            {
                "identity": IDENTITY,
                "state": "prepared",
                "freeze_hash": digest,
                "case_sha256": record["case_sha256"],
                "prepared_at": datetime.now(UTC).isoformat(),
            }
        )
    say(f"freeze record: {digest}")
    say(f"cases: {record['case_count']}")
    for account, amount in sorted(budget.items()):
        say(f"estimated admission budget, {account}: {amount:.4f}")
    say("nothing was sent")
    return 0


# -- run -----------------------------------------------------------------------


class RoutingRecorder:
    """Each provider's attempts to its own envelope, and each start and end as
    a whole file in the private folder."""

    def __init__(self, recorders: dict, folder: Path) -> None:
        self._recorders = recorders
        self._folder = folder
        self._provider: dict[str, str] = {}

    def started(self, attempt) -> None:
        recorder = self._recorders[attempt.provider]
        recorder.started(attempt)
        self._provider[attempt.attempt_id] = attempt.provider
        ticket = recorder._reserved[attempt.attempt_id][0]
        write_whole(
            self._folder / f"{attempt.attempt_id}-start.json",
            {**vars(attempt), "ticket": ticket},
        )

    def finished(self, attempt) -> None:
        write_whole(
            self._folder / f"{attempt.attempt_id}-end.json",
            {**vars(attempt), "outcome": attempt.outcome.value},
        )
        self._recorders[self._provider[attempt.attempt_id]].finished(attempt)


async def execute(private: Path, recorder, account_runs, engine_factory=None) -> None:
    """Every case of the snapshot through the composite, each outcome kept."""
    import asyncio

    from app.api.deps import _failover, wire
    from app.application.investigate_dispute import DisputeInvestigator
    from evals import runner

    for run in account_runs:
        if (wait := run.wait()) > 0:
            await asyncio.sleep(wait)
    cases, provider = _cases_and_provider(private)
    engine = (engine_factory or _failover)(recorder=recorder)
    investigator = DisputeInvestigator(provider=provider, engine=wire(engine, provider))
    try:
        for number, case in enumerate(cases, start=1):
            result = await investigator.investigate(_dispute(case), now=runner.EVALUATION_DATE)
            outcome = runner._evaluate(case, result)
            events = [event.event for event in result.events]
            write_whole(
                private / "cases" / f"{number:05d}.json",
                {
                    "number": number,
                    "outcome": vars(outcome),
                    "fully_correct": outcome.ok,
                    "verdict": result.authorization.verdict.value,
                    "engine_called": "decision_engine_skipped" not in events,
                    "outage": "decision_engine_unavailable" in events,
                    "identity_failed": "decision_engine_identity_failed" in events,
                    "invocation_id": result.engine_invocation_id,
                },
            )
    finally:
        await engine.aclose()


def run(args, say, ask=input, engine_factory=None) -> int:
    import asyncio

    from app.decision.base import EngineRejected
    from app.decision.hosted import configuration
    from evals import accounts
    from evals.ledger import LedgerRefused, PrepaidLedger
    from evals.limits import RunStopped
    from evals.service_run import LedgerRecorder
    from evals.state import AccountBusy, AccountRun

    private = Path(args.private)
    if not (private / "snapshot").is_dir():
        raise Refused("nothing was prepared in this folder")
    held = read_state()
    if held is None or held.get("state") != "prepared":
        raise Refused("no prepared reference run: consumed, or never prepared")
    stored = json.loads((private / "freeze.json").read_text(encoding="utf-8"))
    record = freeze_record(private, stored.get("threshold"), stored.get("budget"))
    digest = freeze_hash(record)
    if digest != args.freeze or digest != held.get("freeze_hash"):
        raise Refused("the inputs are not the approved freeze record")
    if record["clean_tree"] is not True or not record["uncertainty_fix"]:
        raise Refused("the source tree is not clean, or lacks the uncertainty fix")

    say(f"reference run: {record['composite']['name']}, {record['composite']['policy']}")
    say(f"cases: {record['case_count']}")
    for account, amount in sorted((record["budget"] or {}).items()):
        say(f"estimated admission budget, {account}: {amount:.4f}")
    say(f"this spends the one reference run. Type {CONFIRMATION} to go on.")
    if ask("> ").strip() != CONFIRMATION:
        say("not confirmed: nothing was sent, the run stays prepared")
        return 2

    configs = [configuration("claude-haiku-5-5"), configuration("gpt-6-luna")]
    began = datetime.now(UTC)
    with contextlib.ExitStack() as held_locks:
        try:
            runs = [
                held_locks.enter_context(AccountRun(accounts.billing_account(config)))
                for config in configs
            ]
        except AccountBusy:
            raise Refused("an account is busy with another run") from None
        ledgers = {
            config.vendor: PrepaidLedger(accounts.billing_account(config), config.currency)
            for config in configs
        }
        with state_lock():
            current = read_state()
            if current is None or current.get("state") != "prepared":
                raise Refused("the reference run was consumed meanwhile")
            if current.get("freeze_hash") != digest:
                raise Refused("the inputs are not the approved freeze record")
            for config in configs:
                left = ledgers[config.vendor].state()["left"]
                if left < (record["budget"] or {}).get(accounts.billing_account(config), 0.0):
                    raise Refused(f"{config.name}: the envelope cannot admit the budget now")
            write_state({**current, "state": "consumed", "consumed_at": began.isoformat()})

        write_whole(private / "status.json", {"state": "running", "started_at": began.isoformat()})
        run_id = f"reference-{began.strftime('%Y%m%dT%H%M%S.%fZ')}"
        recorder = RoutingRecorder(
            {
                config.vendor: LedgerRecorder(
                    ledgers[config.vendor], config, run_id, runs[index].record_send
                )
                for index, config in enumerate(configs)
            },
            private / "attempts",
        )
        status = {"state": "complete"}
        try:
            asyncio.run(execute(private, recorder, runs, engine_factory))
        except (RunStopped, LedgerRefused, EngineRejected) as stop:
            status = {"state": "incomplete", "reason": type(stop).__name__}
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = {"state": "incomplete", "reason": "interrupted"}
        except Exception as defect:
            status = {"state": "incomplete", "reason": f"internal error ({type(defect).__name__})"}
            traceback.print_exc()
        write_whole(private / "status.json", {**status, "started_at": began.isoformat()})
    totals = compute_totals(private)
    write_whole(private / "totals.json", totals)
    report(totals, say)
    return 0 if totals["complete"] else 3


# -- totals ----------------------------------------------------------------------


def compute_totals(private: Path) -> dict:
    """From the records alone: no input reopened, no case re-scored."""
    freeze = json.loads((private / "freeze.json").read_text(encoding="utf-8"))
    status_path = private / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    cases = _read_all(private / "cases")
    starts = {r["attempt_id"]: r for r in _read_all(private / "attempts") if "ticket" in r}
    ends = {r["attempt_id"]: r for r in _read_all(private / "attempts") if "ticket" not in r}

    by_invocation: dict[str, list[dict]] = {}
    for attempt_id, start in starts.items():
        by_invocation.setdefault(start["invocation_id"], []).append(
            {**start, **ends.get(attempt_id, {"outcome": None})}
        )
    fell_back = both_failed = 0
    for attempts in by_invocation.values():
        second = [a for a in attempts if a["number"] == 2]
        if second:
            if second[0]["outcome"] == "answered":
                fell_back += 1
            else:
                both_failed += 1

    answered: dict[str, int] = {}
    for end in ends.values():
        if end["outcome"] == "answered" and end.get("returned_model"):
            answered[end["returned_model"]] = answered.get(end["returned_model"], 0) + 1
    spend: dict[str, dict] = {}
    for attempt_id, start in starts.items():
        entry = spend.setdefault(start["provider"], {"settled_usd": 0.0, "unknown_attempts": 0})
        cost = ends.get(attempt_id, {}).get("cost_micros")
        if cost is None:
            entry["unknown_attempts"] += 1
        else:
            entry["settled_usd"] = round(entry["settled_usd"] + cost / 1e6, 6)

    complete = status.get("state") == "complete" and len(cases) == freeze["case_count"]
    to_person = [c for c in cases if c["verdict"] in {"human_review", "requires_approval"}]
    return {
        "complete": complete,
        "stop": None if complete else status.get("reason", "the run never ended on record"),
        "cases_total": freeze["case_count"],
        "cases_run": len(cases),
        "cases_not_run": freeze["case_count"] - len(cases),
        "rules_only": sum(1 for c in cases if not c["engine_called"]),
        "fully_correct": sum(1 for c in cases if c["fully_correct"]),
        "unsafe": sum(1 for c in cases if c["outcome"].get("unsafe")),
        "sent_to_a_person": len(to_person),
        "of_which_outage": sum(1 for c in to_person if c["outage"]),
        "of_which_identity_failure": sum(1 for c in to_person if c["identity_failed"]),
        "luna_failed_haiku_answered": fell_back,
        "both_failed": both_failed,
        "invalid_answers": sum(1 for e in ends.values() if e["outcome"] == "unusable"),
        "answered_by": answered,
        "spend": spend,
        "threshold": freeze.get("threshold"),
    }


def report(totals: dict, say) -> None:
    if totals["unsafe"]:
        say(f"UNSAFE ACTIONS: {totals['unsafe']} -- adoption paused for the maintainer's review")
    if not totals["complete"]:
        say(f"INCOMPLETE ({totals['stop']}): no figure below is over the full set")
        say(f"cases run: {totals['cases_run']} of {totals['cases_total']}")
        say(f"fully correct among those run: {totals['fully_correct']}")
    else:
        say(f"fully correct: {totals['fully_correct']} of {totals['cases_total']}")
    for key in (
        "rules_only",
        "sent_to_a_person",
        "of_which_outage",
        "of_which_identity_failure",
        "luna_failed_haiku_answered",
        "both_failed",
        "invalid_answers",
    ):
        say(f"{key.replace('_', ' ')}: {totals[key]}")
    for model, count in sorted(totals["answered_by"].items()):
        say(f"answered by {model}: {count}")
    for provider, entry in sorted(totals["spend"].items()):
        say(
            f"spend, {provider}: {entry['settled_usd']:.4f} USD settled, "
            f"{entry['unknown_attempts']} attempts of unknown cost"
        )
    if totals["threshold"] is not None:
        say(f"declared threshold: {totals['threshold']}")


def rebuild(args, say) -> int:
    private = Path(args.private)
    totals = compute_totals(private)
    write_whole(private / "totals.json", totals)
    report(totals, say)
    return 0 if totals["complete"] else 3


# -- entry -----------------------------------------------------------------------


def main(argv: list[str] | None = None, descriptors: bool = True, **hooks) -> int:
    parser = argparse.ArgumentParser(prog="evals.reference_run")
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--cases", required=True)
    prep.add_argument("--fixtures", required=True)
    prep.add_argument("--private", required=True)
    prep.add_argument("--threshold", type=float, default=None)
    go = commands.add_parser("run")
    go.add_argument("--freeze", required=True)
    go.add_argument("--private", required=True)
    again = commands.add_parser("rebuild")
    again.add_argument("--private", required=True)
    args = parser.parse_args(argv)

    try:
        refuse_tracing()
        args.private = str(private_folder(args.private))
    except Refused as refusal:
        print(f"refused: {refusal}")
        return 2
    with Lockdown(Path(args.private), descriptors=descriptors) as locked:
        try:
            import app.decision.anthropic_hosted  # noqa: F401
            import app.decision.openai_compatible  # noqa: F401
            from app.decision.hosted import silence_request_logging

            for name in ("openai._base_client", "anthropic._base_client", "httpx", "httpcore"):
                silence_request_logging(name)
            refuse_tracing()
            if args.command == "prepare":
                return prepare(args, locked.say, hooks.get("legs"))
            if args.command == "run":
                return run(args, locked.say, hooks.get("ask", input), hooks.get("engine_factory"))
            return rebuild(args, locked.say)
        except Refused as refusal:
            locked.say(f"refused: {refusal}")
            return 2
        except Exception as defect:
            traceback.print_exc()
            locked.say(f"stopped: {type(defect).__name__}. The private record: {args.private}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

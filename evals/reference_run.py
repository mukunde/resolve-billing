"""The holdout's reference run, once, on the adopted composite
(specs/011-reference-run).

Run by the maintainer alone, never by an agent on the real holdout. Three
commands:

    python -m dotenv run -- python -m evals.reference_run prepare \\
        --cases PATH --fixtures DIR --private DIR [--threshold N]
    python -m dotenv run -- python -m evals.reference_run run --freeze HASH --private DIR
    python -m evals.reference_run rebuild --private DIR

`prepare` copies every input into the private folder and writes the freeze
record there, sending nothing. `run`, after the maintainer types RUN ONCE,
spends the one authorisation before its first send, copies the approved inputs
into a folder of its own, and runs them through `gpt-6-luna+claude-haiku-5-5`,
fallback on. `rebuild` recomputes the totals from that run's records, calling
nothing.

Constitution, principle IX: a session that tunes the system does not read the
cases. So nothing case-level leaves the private folder: before anything else,
standard output, standard error, their file descriptors and every logger go to
the folder's log, and only an allowlist -- the summary, the prompt, the totals,
a refusal's category -- reaches the console. Only the standard library is
imported before that, and an argument error says only that it is one.
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
#: The configurations of the composite, in the order their locks are taken.
CONFIGURATIONS = ("claude-haiku-5-5", "gpt-6-luna")


class Refused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The run may not go on. Its text is a category, never data."""


def _console(line: str) -> None:
    """The console before the lockdown: a category, never data."""
    print(line, file=sys.__stdout__, flush=True)


class _Parser(argparse.ArgumentParser):
    """An argument error names no value: a path or a number given here could
    be a case's."""

    def error(self, message: str):  # noqa: ARG002 -- the message quotes the arguments
        _console("refused: invalid arguments")
        raise SystemExit(2)


# -- lockdown ------------------------------------------------------------------


class Lockdown:
    """Every output into the private log, but an allowlisted console."""

    def __init__(self, private: Path, descriptors: bool = True) -> None:
        self.private = private
        self.log_path = private / "log.txt"
        self._descriptors = descriptors
        self._saved: list = []
        self.console = None

    def __enter__(self) -> Lockdown:
        contained(self.log_path, self.private)
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
    for parent in (folder, *folder.parents):
        if (parent / ".git").exists():
            raise Refused("the private folder is inside a repository")
    for tree in _worktrees():
        if _same(folder, tree) or any(_same(tree, parent) for parent in folder.parents):
            raise Refused("the private folder is inside a worktree")
    try:
        # A new file of its own: nothing existing is followed or truncated.
        handle, probe = tempfile.mkstemp(dir=folder, prefix=".write-probe-")
        os.close(handle)
        os.unlink(probe)
    except OSError:
        raise Refused("the private folder is not writable") from None
    return folder


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


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


def contained(path: Path, private: Path) -> Path:
    """A destination inside the private folder, through no link: refused
    otherwise, before anything is written."""
    for part in (path, *path.parents):
        if _same(part, private):
            break
        if part.is_symlink():
            raise Refused("a link inside the private folder")
    resolved = path.resolve()
    if not (_same(resolved, private) or any(_same(p, private) for p in resolved.parents)):
        raise Refused("a destination outside the private folder")
    return path


# -- the record files ----------------------------------------------------------


def write_whole(path: Path, content: dict, private: Path) -> None:
    """A whole file or none: written aside, synced, renamed, folder synced."""
    contained(path.parent, private)
    path.parent.mkdir(parents=True, exist_ok=True)
    contained(path, private)
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
    try:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(folder.glob("*.json"))
            if not path.name.startswith(".")
        ]
    except (OSError, ValueError):
        raise Refused("a record is unreadable") from None


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
    from evals.state import write_atomically

    write_atomically(state_path(), content)


# -- inputs --------------------------------------------------------------------


def copy_inputs(cases: Path, fixtures: Path, target: Path, private: Path) -> None:
    """The case file, the repository's fixtures as `base/` and the holdout's
    as `extra/`, copied into a new folder."""
    from app.providers.fake.fixtures import FIXTURES_DIR

    contained(target, private)
    (target / "base").mkdir(parents=True)
    (target / "extra").mkdir()
    (target / "cases.json").write_bytes(cases.read_bytes())
    for name in FIXTURE_NAMES:
        (target / "base" / name).write_bytes((FIXTURES_DIR / name).read_bytes())
    for path in sorted(fixtures.iterdir()):
        if path.is_dir():
            raise Refused("the holdout's fixtures hold a folder")
        (target / "extra" / path.name).write_bytes(path.read_bytes())


def inputs_hashes(target: Path) -> dict[str, str]:
    return {
        str(path.relative_to(target)).replace("\\", "/"): _sha256(path.read_bytes())
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }


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


def accounts_frozen() -> dict:
    """Each configuration's declared account and billing account."""
    from app.decision.hosted import configuration
    from evals import accounts

    held = {}
    for name in CONFIGURATIONS:
        config = configuration(name)
        try:
            accounts.declared(config)
            held[name] = {
                "identity": accounts.identity(config),
                "billing_account": accounts.billing_account(config),
            }
        except accounts.AccountRefused:
            raise Refused(f"{name}: the account is not declared") from None
    return held


def freeze_record(target: Path, threshold: float | None, budget: dict, accounts_held: dict) -> dict:
    """What the run is bound to, read from a copy of the inputs."""
    from dataclasses import asdict

    from app.api.deps import FAILOVER, HAIKU, LUNA
    from app.decision.failover import DEADLINE, POLICY, PRIMARY_BOUNDS, SECONDARY_BOUNDS
    from app.decision.hosted import PROMPT_VERSION, configuration
    from app.decision.masking import MASKING_VERSION
    from app.decision.verdict import verdict_schema
    from evals import runner
    from evals.first_calls import fingerprint

    cases = json.loads((target / "cases.json").read_text(encoding="utf-8"))
    files = inputs_hashes(target)
    return {
        "identity": IDENTITY,
        **repository_state(),
        "configurations": {name: fingerprint(configuration(name)) for name in (LUNA, HAIKU)},
        "accounts": accounts_held,
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


def _cases_and_provider(target: Path):
    from app.providers.fake.adapter import FakeBillingProvider
    from app.providers.fake.fixtures import load_from

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


async def estimate(target: Path, legs) -> dict[str, float]:
    from app.api.deps import wire
    from app.application.investigate_dispute import DisputeInvestigator
    from evals import runner

    cases, provider = _cases_and_provider(target)
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


def admissible(budget: dict[str, float], accounts_held: dict, ledgers=None) -> None:
    """Both accounts as frozen, no ambient setting, both first calls accepted,
    and each envelope's room for its own budget."""
    from app.decision import anthropic_hosted, openai_compatible
    from app.decision.hosted import configuration
    from evals import accounts
    from evals.first_calls import FirstCalls, FirstCallsRefused
    from evals.ledger import LedgerRefused, PrepaidLedger

    if accounts_frozen() != accounts_held:
        raise Refused("the declared accounts are not the frozen ones")
    for module in (anthropic_hosted, openai_compatible):
        if module.ambient_refusal() is not None:
            raise Refused("an ambient SDK setting is set")
    for name in CONFIGURATIONS:
        config = configuration(name)
        account = accounts_held[name]["billing_account"]
        if account not in budget:
            raise Refused(f"{name}: no budget was frozen for its account")
        try:
            FirstCalls().admit_full_run(config)
            ledger = (ledgers or {}).get(account) or PrepaidLedger(account, config.currency)
            left = ledger.state()["left"]
        except (accounts.AccountRefused, FirstCallsRefused, LedgerRefused) as refusal:
            raise Refused(f"{name}: {type(refusal).__name__}") from None
        if left < budget[account]:
            raise Refused(f"{name}: the envelope cannot admit the estimated budget")


def prepare(args, say, legs=None) -> int:
    import asyncio
    import shutil
    import uuid

    from evals.check import check

    private = Path(args.private)
    with state_lock():
        held = read_state()
        if held is not None and held.get("state") != "prepared":
            raise Refused("the reference run was already consumed")
    staging = private / f".staging-{uuid.uuid4().hex}"
    try:
        copy_inputs(Path(args.cases), Path(args.fixtures), staging, private)
        if check(staging / "cases.json", fixtures=(staging / "base", staging / "extra")) != 0:
            raise Refused("the check failed")
        state = repository_state()
        if state["clean_tree"] is not True:
            raise Refused("the source tree is not clean")
        if not state["uncertainty_fix"]:
            raise Refused("the commit does not contain the uncertainty fix")
        accounts_held = accounts_frozen()
        budget = asyncio.run(estimate(staging, legs if legs is not None else legs_for_budget()))
        admissible(budget, accounts_held)
        record = freeze_record(staging, args.threshold, budget, accounts_held)
        digest = freeze_hash(record)
        with state_lock():
            held = read_state()
            if held is not None and held.get("state") != "prepared":
                raise Refused("the reference run was already consumed")
            if held is not None and held.get("freeze_hash") != digest:
                raise Refused("a reference run was prepared on other inputs")
            snapshot = private / "snapshot"
            contained(snapshot, private)
            if snapshot.exists():
                shutil.rmtree(snapshot)
            os.replace(staging, snapshot)
            write_whole(private / "freeze.json", record, private)
            write_state(
                {
                    "identity": IDENTITY,
                    "state": "prepared",
                    "freeze_hash": digest,
                    "case_sha256": record["case_sha256"],
                    "prepared_at": datetime.now(UTC).isoformat(),
                }
            )
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    say(f"freeze record: {digest}")
    say(f"cases: {record['case_count']}")
    for account, amount in sorted(budget.items()):
        say(f"estimated admission budget, {account}: {amount:.4f}")
    say("nothing was sent")
    return 0


# -- run -----------------------------------------------------------------------


class RoutingRecorder:
    """Each provider's attempts to its own envelope, and each start and end as
    a whole file of the run, bound to it, with the ledger's state."""

    def __init__(self, recorders: dict, folder: Path, private: Path, run_id: str) -> None:
        self._recorders = recorders
        self._folder = folder
        self._private = private
        self._run_id = run_id
        self._provider: dict[str, str] = {}

    def _ticket(self, attempt_id: str) -> str:
        # The LedgerRecorder's own ticket: deterministic, so a rebuild can match it.
        return f"{self._run_id}/{attempt_id}"

    def started(self, attempt) -> None:
        recorder = self._recorders[attempt.provider]
        self._provider[attempt.attempt_id] = attempt.provider
        # Before the reservation: a crash after it leaves a start with no end,
        # read as possibly spent.
        write_whole(
            self._folder / f"{attempt.attempt_id}-start.json",
            {**vars(attempt), "run_id": self._run_id, "ticket": self._ticket(attempt.attempt_id)},
            self._private,
        )
        try:
            recorder.started(attempt)
        except BaseException:
            # The reservation may have been made before the failure: its state
            # is read from the recorder, never assumed.
            reserved = attempt.attempt_id in recorder._reserved
            write_whole(
                self._folder / f"{attempt.attempt_id}-end.json",
                {
                    "attempt_id": attempt.attempt_id,
                    "run_id": self._run_id,
                    "outcome": "interrupted" if reserved else "not_reserved",
                    "ledger": "open" if reserved else "none",
                    "settled_usd": None if reserved else 0.0,
                },
                self._private,
            )
            raise

    def finished(self, attempt) -> None:
        recorder = self._recorders[self._provider[attempt.attempt_id]]
        ticket = self._ticket(attempt.attempt_id)
        before = recorder.cost
        try:
            recorder.finished(attempt)
        finally:
            settled = ticket not in recorder.open_reservations
            write_whole(
                self._folder / f"{attempt.attempt_id}-end.json",
                {
                    **vars(attempt),
                    "outcome": attempt.outcome.value,
                    "run_id": self._run_id,
                    "ledger": "settled" if settled else "open",
                    "settled_usd": round(recorder.cost - before, 6) if settled else None,
                },
                self._private,
            )


async def execute(run_dir: Path, private: Path, run_id: str, recorder, account_runs, factory=None):
    """Every case of the run's own copy through the composite, each kept."""
    import asyncio

    from app.api.deps import _failover, wire
    from app.application.investigate_dispute import DisputeInvestigator
    from evals import runner

    for account_run in account_runs:
        if (wait := account_run.wait()) > 0:
            await asyncio.sleep(wait)
    cases, provider = _cases_and_provider(run_dir / "inputs")
    engine = (factory or _failover)(recorder=recorder)
    investigator = DisputeInvestigator(provider=provider, engine=wire(engine, provider))
    try:
        for number, case in enumerate(cases, start=1):
            result = await investigator.investigate(_dispute(case), now=runner.EVALUATION_DATE)
            outcome = runner._evaluate(case, result)
            events = [event.event for event in result.events]
            write_whole(
                run_dir / "cases" / f"{number:05d}.json",
                {
                    "run_id": run_id,
                    "number": number,
                    "outcome": vars(outcome),
                    "fully_correct": outcome.ok,
                    "verdict": result.authorization.verdict.value,
                    "engine_called": "decision_engine_skipped" not in events,
                    "outage": "decision_engine_unavailable" in events,
                    "identity_failed": "decision_engine_identity_failed" in events,
                    "invocation_id": result.engine_invocation_id,
                },
                private,
            )
    finally:
        await engine.aclose()


def run(args, say, ask=input, engine_factory=None) -> int:
    import shutil

    private = Path(args.private)
    snapshot = private / "snapshot"
    if not snapshot.is_dir():
        raise Refused("nothing was prepared in this folder")
    held = read_state()
    if held is None or held.get("state") != "prepared":
        raise Refused("no prepared reference run: consumed, or never prepared")
    stored = json.loads((private / "freeze.json").read_text(encoding="utf-8"))
    if freeze_hash(stored) != args.freeze or args.freeze != held.get("freeze_hash"):
        raise Refused("the inputs are not the approved freeze record")

    began = datetime.now(UTC)
    run_id = f"reference-{began.strftime('%Y%m%dT%H%M%S.%fZ')}"
    run_dir = private / run_id
    pending = private / f".pending-{run_id}"
    contained(run_dir, private)
    contained(pending, private)
    if any(path.is_dir() for path in private.glob("reference-*")):
        raise Refused("this folder already holds a run")
    pending.mkdir()
    try:
        return _run_from(args, say, ask, engine_factory, private, stored, began, run_id, pending)
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def _run_from(args, say, ask, engine_factory, private, stored, began, run_id, pending) -> int:
    import asyncio
    import shutil

    from app.decision.base import EngineRejected
    from app.decision.hosted import configuration
    from evals.ledger import LedgerRefused, PrepaidLedger
    from evals.limits import RunStopped
    from evals.service_run import LedgerRecorder
    from evals.state import AccountBusy, AccountRun

    run_dir = private / run_id
    # The run's own copy, checked against the freeze record: whatever happens to
    # the snapshot from now on, these are the bytes that run.
    shutil.copytree(private / "snapshot", pending / "inputs")
    record = freeze_record(
        pending / "inputs", stored.get("threshold"), stored.get("budget"), stored.get("accounts")
    )
    if freeze_hash(record) != args.freeze:
        raise Refused("the inputs are not the approved freeze record")
    if record["clean_tree"] is not True or not record["uncertainty_fix"]:
        raise Refused("the source tree is not clean, or lacks the uncertainty fix")

    say(f"reference run: {record['composite']['name']}, {record['composite']['policy']}")
    say(f"cases: {record['case_count']}")
    for account, amount in sorted(record["budget"].items()):
        say(f"estimated admission budget, {account}: {amount:.4f}")
    say(f"this spends the one reference run. Type {CONFIRMATION} to go on.")
    if ask("> ").strip() != CONFIRMATION:
        say("not confirmed: nothing was sent, the run stays prepared")
        return 2

    configs = [configuration(name) for name in CONFIGURATIONS]
    accounts_held = record["accounts"]
    with contextlib.ExitStack() as locks:
        try:
            runs = [
                locks.enter_context(AccountRun(accounts_held[config.name]["billing_account"]))
                for config in configs
            ]
        except AccountBusy:
            raise Refused("an account is busy with another run") from None
        ledgers = {
            accounts_held[config.name]["billing_account"]: PrepaidLedger(
                accounts_held[config.name]["billing_account"], config.currency
            )
            for config in configs
        }
        with state_lock():
            current = read_state()
            if current is None or current.get("state") != "prepared":
                raise Refused("the reference run was consumed meanwhile")
            if current.get("freeze_hash") != args.freeze:
                raise Refused("the inputs are not the approved freeze record")
            admissible(record["budget"], accounts_held, ledgers)
            write_state(
                {
                    **current,
                    "state": "consumed",
                    "consumed_at": began.isoformat(),
                    "run_id": run_id,
                }
            )
        # Spent: the copy becomes the run's folder, the only one this folder holds.
        os.replace(pending, run_dir)

        write_whole(
            run_dir / "status.json",
            {"run_id": run_id, "state": "running", "started_at": began.isoformat()},
            private,
        )
        recorder = RoutingRecorder(
            {
                config.vendor: LedgerRecorder(
                    ledgers[accounts_held[config.name]["billing_account"]],
                    config,
                    run_id,
                    runs[index].record_send,
                    service_like=True,
                )
                for index, config in enumerate(configs)
            },
            run_dir / "attempts",
            private,
            run_id,
        )
        status = {"state": "complete"}
        try:
            asyncio.run(execute(run_dir, private, run_id, recorder, runs, engine_factory))
        except (RunStopped, LedgerRefused, EngineRejected) as stop:
            status = {"state": "incomplete", "reason": type(stop).__name__}
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = {"state": "incomplete", "reason": "interrupted"}
        except Exception as defect:
            status = {"state": "incomplete", "reason": f"internal error ({type(defect).__name__})"}
            traceback.print_exc()
        write_whole(
            run_dir / "status.json",
            {**status, "run_id": run_id, "started_at": began.isoformat()},
            private,
        )
    totals = compute_totals(private)
    write_whole(run_dir / "totals.json", totals, private)
    report(totals, say)
    return 0 if totals["complete"] else 3


# -- totals ----------------------------------------------------------------------


def the_run(private: Path) -> Path:
    runs = [path for path in private.glob("reference-*") if path.is_dir()]
    if len(runs) != 1:
        raise Refused("the private folder must hold exactly one run")
    return runs[0]


def compute_totals(private: Path) -> dict:
    """From the run's records alone: no input reopened, no case re-scored.
    Records not bound to the run, duplicated or out of sequence refuse."""
    run_dir = the_run(private)
    run_id = run_dir.name
    freeze = json.loads((private / "freeze.json").read_text(encoding="utf-8"))
    status_path = run_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    cases = _read_all(run_dir / "cases")
    attempts = _read_all(run_dir / "attempts")
    if any(record.get("run_id") != run_id for record in [*cases, *attempts]):
        raise Refused("a record does not belong to this run")
    numbers = [case["number"] for case in cases]
    if numbers != list(range(1, len(cases) + 1)) or len(cases) > freeze["case_count"]:
        raise Refused("the case records are duplicated or out of sequence")
    start_list = [r for r in attempts if "ticket" in r]
    end_list = [r for r in attempts if "ticket" not in r]
    starts = {r["attempt_id"]: r for r in start_list}
    ends = {r["attempt_id"]: r for r in end_list}
    if len(starts) != len(start_list) or len(ends) != len(end_list):
        raise Refused("an attempt is recorded twice")
    if set(ends) - set(starts):
        raise Refused("an attempt ends without a start")

    second_leg = {s["invocation_id"]: s["attempt_id"] for s in starts.values() if s["number"] == 2}
    fell_back = both_failed = 0
    for case in cases:
        attempt = second_leg.get(case["invocation_id"])
        if attempt is None:
            continue
        if ends.get(attempt, {}).get("outcome") == "answered" and not case["outage"]:
            fell_back += 1
        elif case["outage"]:
            both_failed += 1

    answered: dict[str, int] = {}
    for end in ends.values():
        if end["outcome"] == "answered" and end.get("returned_model"):
            answered[end["returned_model"]] = answered.get(end["returned_model"], 0) + 1
    spend: dict[str, dict] = {}
    open_attempts = 0
    for attempt_id, start in starts.items():
        entry = spend.setdefault(start["provider"], {"settled_usd": 0.0, "unknown_attempts": 0})
        end = ends.get(attempt_id)
        if end is None or end["ledger"] == "open":
            entry["unknown_attempts"] += 1
            open_attempts += end is None
        else:
            entry["settled_usd"] = round(entry["settled_usd"] + (end["settled_usd"] or 0.0), 6)

    complete = (
        status.get("state") == "complete"
        and status.get("run_id") == run_id
        and len(cases) == freeze["case_count"]
    )
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
        "unfinished_attempts": open_attempts,
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
        "unfinished_attempts",
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
    write_whole(the_run(private) / "totals.json", totals, private)
    report(totals, say)
    return 0 if totals["complete"] else 3


# -- entry -----------------------------------------------------------------------


def main(argv: list[str] | None = None, descriptors: bool = True, **hooks) -> int:
    parser = _Parser(prog="evals.reference_run", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    prep = commands.add_parser("prepare", add_help=False)
    prep.add_argument("--cases", required=True)
    prep.add_argument("--fixtures", required=True)
    prep.add_argument("--private", required=True)
    prep.add_argument("--threshold", type=float, default=None)
    go = commands.add_parser("run", add_help=False)
    go.add_argument("--freeze", required=True)
    go.add_argument("--private", required=True)
    again = commands.add_parser("rebuild", add_help=False)
    again.add_argument("--private", required=True)
    args = parser.parse_args(argv)

    try:
        refuse_tracing()
        args.private = str(private_folder(args.private))
        lockdown = Lockdown(Path(args.private), descriptors=descriptors)
        locked = lockdown.__enter__()
    except Refused as refusal:
        _console(f"refused: {refusal}")
        return 2
    except (OSError, KeyboardInterrupt):
        _console("refused: the private folder cannot be used")
        return 2
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
    except BaseException as defect:
        traceback.print_exc()
        locked.say(f"stopped: {type(defect).__name__}. The private record: {args.private}")
        return 1
    finally:
        lockdown.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())

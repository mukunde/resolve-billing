"""A run of an engine evaluated on a free tier (specs/004-second-engines).

The runner's path for the three configurations, on the development cases
alone (the guard in `evals/engines.py` has already held). What it adds to an
ordinary run: the engine held to its limits (`evals/governed.py`), the
Mistral envelope (`evals/ledger.py`), a status per case, and a result that
says whether the run went to its end.

A run stopped before its end -- the envelope, the deadline, rate limiting
outlasting the attempts, usage the reservation did not bound, a refusal, an
interruption -- is still written, with `complete: false`, the cases not
attempted and the reason, and exits non-zero; its totals are over the cases
it scored, never over the whole set. A result never overwrites an earlier
one: each file carries its run's start time.

Every score here is a regression score on the development cases: those
cases shaped the prompt, so it says whether an engine holds them, not how it
would do on disputes it has never seen.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.decision.base import EngineRejected
from app.decision.hosted import GEMINI_PRICES, configuration
from app.decision.masking import MASKING_VERSION
from app.decision.verdict import verdict_schema
from evals import engines
from evals.first_calls import QUOTA_PROJECT, FirstCalls, FirstCallsRefused
from evals.governed import Governed
from evals.ledger import Ledger, LedgerRefused
from evals.limits import RunStopped, limiters_for
from evals.quota import DailyQuota, QuotaRefused
from evals.state import AccountBusy, AccountRun

QUOTA_ASSUMPTIONS = (
    "dedicated project, no other caller; every attempt counted, never refunded; "
    "the key's project as declared by the maintainer, not verified"
)

STATEMENT = (
    "regression score on development cases: these cases shaped the prompt; the score says "
    "whether the engine holds them, not how it would do on unseen disputes"
)

#: Statuses whose case is not a decision of the model, and is never scored.
NOT_SCORED = frozenset({"not_sent_oversized", "unavailable"})


@dataclass
class HostedRun:
    engine_name: str
    started_at: datetime
    total: int
    outcomes: list = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    complete: bool = False
    stop_reason: str | None = None
    stopped_at: str | None = None
    not_attempted: list[str] = field(default_factory=list)
    governed: Governed | None = None
    finished: float | None = None

    def stop(self, reason: str, cases: list[dict], index: int) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stopped_at = cases[index]["id"]
        self.not_attempted = [case["id"] for case in cases[index + 1 :]]

    @property
    def scored(self) -> list:
        return [o for o in self.outcomes if self.statuses.get(o.case_id) not in NOT_SCORED]


async def run(
    record: HostedRun,
    cases: list[dict],
    ledger: Ledger | None,
    account: AccountRun,
    quota: DailyQuota | None = None,
    first_call_only: bool = False,
) -> None:
    """Run the cases into `record`, which holds what was done whatever stops
    the run. `account` is held: no other run shares its quota meanwhile. With
    `first_call_only`, a smoke test: one attempt on the first case that
    reaches the engine, then the run stops, whatever came of it."""
    from app.api.deps import wire
    from app.application.investigate_dispute import DisputeInvestigator
    from app.domain.models import Dispute
    from app.providers.fake.adapter import FakeBillingProvider
    from evals import runner

    config = configuration(record.engine_name)
    provider = FakeBillingProvider()
    # Nothing is attempted until a case begins: whatever stops the run
    # before then leaves every case unattempted.
    record.not_attempted = [case["id"] for case in cases]
    # The previous run on this account may still count against the minute.
    if (wait := account.wait()) > 0:
        print(f"waiting {wait:.0f} s for the previous run's quota window", file=sys.stderr)
        await asyncio.sleep(wait)
    governed = Governed(
        engines.HOSTED_ENGINES[record.engine_name](),
        # One run per account at a time: the limiter Mistral's models share
        # is this run's alone.
        limiters_for(config, shared={}),
        ledger,
        attempts=1 if first_call_only else None,
        run_id=record.started_at.strftime("%Y%m%dT%H%M%S.%fZ"),
        on_send=account.record_send,
        quota=quota,
    )
    record.governed = governed
    investigator = DisputeInvestigator(provider=provider, engine=wire(governed, provider))
    try:
        for index, case in enumerate(cases):
            record.not_attempted = [later["id"] for later in cases[index + 1 :]]
            record.stopped_at = case["id"]
            governed.begin(case["id"])
            dispute = Dispute(
                id=f"eval_{case['id']}",
                customer_message=case["message"],
                customer_email=case["customer_email"],
                customer_id=case.get("customer_id"),
                invoice_id=case["invoice_id"],
                headers=runner.header_pairs(case.get("headers")),
            )
            try:
                result = await investigator.investigate(dispute, now=runner.EVALUATION_DATE)
            except RunStopped as stop:
                record.stop(str(stop), cases, index)
                return
            except EngineRejected as refusal:
                record.stop(f"the engine refused the request ({refusal})", cases, index)
                return
            except LedgerRefused as refusal:
                record.stop(f"the ledger refused: {refusal}", cases, index)
                return
            except QuotaRefused as refusal:
                record.stop(f"the quota refused: {refusal}", cases, index)
                return
            except (KeyboardInterrupt, asyncio.CancelledError):
                record.stop("interrupted", cases, index)
                raise
            except Exception as error:
                # A defect: recorded by its type alone, then raised as itself.
                record.stop(f"internal error ({type(error).__name__})", cases, index)
                raise
            record.stopped_at = None
            record.outcomes.append(runner._evaluate(case, result))
            # A case the rules settled never entered the engine.
            record.statuses[case["id"]] = governed.statuses.get(case["id"], "rules_only")
            if first_call_only and case["id"] in governed.statuses:
                versions = governed.engine.model_versions
                record.stop_reason = _first_call_reason(
                    governed.statuses[case["id"]],
                    governed.outages.get(case["id"]),
                    versions[-1] if versions else None,
                )
                record.stopped_at = case["id"]
                return
        record.complete = True
    finally:
        record.finished = time.monotonic()
        await governed.aclose()


#: A first call that opens the way to the full run (FR-016).
ACCEPTED = "first call accepted"


def _first_call_reason(status: str, outage: str | None, model_version: str | None) -> str:
    """Accepted on a valid verdict -- its usage already checked, since a
    missing or excessive usage stops the run before a status is set -- with
    a model version returned; otherwise, why not."""
    if status in {"decided", "abstained"}:
        return ACCEPTED if model_version else "first call not accepted: no model version returned"
    if status == "invalid":
        return "first call not accepted: invalid answer"
    if status == "not_sent_oversized":
        return "not sent: oversized"
    if outage is not None:
        return f"outage: {outage}"
    return f"first call {status}"


def _admitted(quota: DailyQuota, args, config) -> None:
    """Today declared -- already, or by the flag -- and the run admitted."""
    if args.gemini_requests_used_today is not None:
        quota.declare(args.gemini_requests_used_today)
    if args.first_call_only:
        quota.admit_first_call()
    else:
        quota.admit_full_run(engines.ENGINE_CASES * config.attempts)


def main(args, cases: list[dict], parser) -> int:
    from evals import runner

    if args.data_terms is None:
        parser.error(
            f"--data-terms is required for {args.engine}: one of {', '.join(runner.DATA_TERMS)}"
        )
    if args.json is None:
        parser.error(f"--json is required for {args.engine}: its run is always kept")
    if args.runs != 1 or args.baseline or args.write_baseline or args.min_fully_correct:
        parser.error(f"{args.engine} runs once, with no baseline and no floor")
    config = configuration(args.engine)
    if config.vendor != "mistral" and args.mistral_allowance_usd is not None:
        parser.error("--mistral-allowance-usd is for the Mistral configurations")
    if config.requests_per_day is None and args.gemini_requests_used_today is not None:
        parser.error("--gemini-requests-used-today is for a configuration with a daily limit")
    if (missing := runner.missing_credential(args.engine)) is not None:
        raise SystemExit(missing)
    if not args.first_call_only:
        # A full run only after an accepted first call -- for Mistral, both
        # of its configurations' -- before any client is built (FR-016).
        try:
            FirstCalls().admit_full_run(config)
        except FirstCallsRefused as refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2

    ledger = None
    if config.vendor == "mistral":
        ledger = Ledger()
        try:
            if args.mistral_allowance_usd is not None:
                ledger.declare(args.mistral_allowance_usd)
            ledger.state()
        except LedgerRefused as refusal:
            print(f"refused: {refusal}", file=sys.stderr)
            return 2

    record = HostedRun(
        args.engine,
        datetime.now(UTC),
        total=len(cases),
        not_attempted=[case["id"] for case in cases],
    )
    started = time.monotonic()

    quota = None
    if config.requests_per_day is not None:
        # Before any client is built: the project named, today declared, the
        # run admitted (a full run needs the whole day).
        try:
            quota = DailyQuota(
                os.environ.get(QUOTA_PROJECT, ""),
                config.requests_per_day,
                config.quota_timezone,
                model=config.model,
            )
        except QuotaRefused as refusal:
            print(f"refused: {refusal}; name the project in {QUOTA_PROJECT}", file=sys.stderr)
            return 2
        try:
            _admitted(quota, args, config)
        except QuotaRefused as refusal:
            print(
                f"refused: {refusal}. Declare today's requests already used, as AI Studio "
                "shows them, with --gemini-requests-used-today",
                file=sys.stderr,
            )
            if not args.first_call_only:
                return 2
            # A smoke test refused before sending is still a result: zero sends.
            record.stop_reason = f"not sent: the quota refused ({refusal})"
            written = write(record, args, ledger, started)
            print(f"result written to {written}", file=sys.stderr)
            return 3
    defect = None
    try:
        with AccountRun(config.shared_limits or config.vendor) as account:
            try:
                asyncio.run(run(record, cases, ledger, account, quota, args.first_call_only))
            except (KeyboardInterrupt, asyncio.CancelledError):
                record.stop_reason = record.stop_reason or "interrupted"
                record.complete = False
            except Exception as error:
                defect_reason = f"internal error ({type(error).__name__})"
                record.stop_reason = record.stop_reason or defect_reason
                record.complete = False
                defect = error
    except AccountBusy as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2

    written = write(record, args, ledger, started, quota)
    if args.first_call_only and record.stop_reason == ACCEPTED:
        # Recorded only once its evidence is on disk; an interruption before
        # this line leaves the first call to be run again.
        try:
            FirstCalls().record(config, record.governed.engine.model_versions[-1], written)
        except FirstCallsRefused as refusal:
            print(f"the first call was accepted but not recorded: {refusal}", file=sys.stderr)
            print(f"result written to {written}", file=sys.stderr)
            return 3
    if defect is not None:
        print(f"result written to {written}", file=sys.stderr)
        raise defect
    print(f"\n{STATEMENT}")
    if not record.complete:
        print(
            f"INCOMPLETE: stopped at {record.stopped_at} ({record.stop_reason}); "
            f"{len(record.outcomes)} cases run, {len(record.not_attempted)} not attempted. "
            "The totals below are over the cases scored, not over the set.",
            file=sys.stderr,
        )
    not_scored = [o.case_id for o in record.outcomes if o not in record.scored]
    if not_scored:
        print(f"not scored (no decision of the model): {', '.join(not_scored)}")
    status = runner.report(args.engine, record.scored)
    print(f"\nresult written to {written}")
    return status if record.complete else 3


def write(
    record: HostedRun,
    args,
    ledger: Ledger | None,
    started: float,
    quota: DailyQuota | None = None,
) -> Path:
    config = configuration(record.engine_name)
    governed = record.governed
    if config.price_per_mtok is None:
        cost = {"basis": "estimated zero under the free quota", "source": GEMINI_PRICES}
    else:
        cost = {
            "basis": "estimated from the reported usage at list prices",
            "usd": round(governed.cost_usd, 6) if governed else 0.0,
            "source": config.price_source,
        }
        if ledger is not None:
            try:
                cost["envelope"] = ledger.state()
            except LedgerRefused as refusal:
                cost["envelope"] = f"unreadable: {refusal}"
    finished = record.finished or time.monotonic()
    payload = {
        "engine": record.engine_name,
        "engine_version": config.prompt_version,
        # The bytes the guard checked and the run executed, not the file as
        # it may stand by now.
        "cases_file": engines.DEVELOPMENT_SET.name,
        "cases_sha256": engines.DEVELOPMENT_SET_SHA256[:16],
        "statement": STATEMENT,
        "ran_at": record.started_at.isoformat(),
        "complete": record.complete,
        "stop_reason": record.stop_reason,
        "stopped_at": record.stopped_at,
        "cases_total": record.total,
        "cases_run": len(record.outcomes),
        "not_attempted": record.not_attempted,
        "not_scored": [o.case_id for o in record.outcomes if o not in record.scored],
        "configuration": asdict(config),
        "masking_version": MASKING_VERSION,
        "schema_sha256": hashlib.sha256(
            json.dumps(verdict_schema(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "model_versions": sorted(set(governed.engine.model_versions)) if governed else [],
        "vendor_notes": sorted(set(governed.engine.notes)) if governed else [],
        "data_terms": args.data_terms,
        "statuses": record.statuses,
        "attempts": governed.attempts if governed else {},
        "count_requests": governed.counts if governed else {},
        "limiter_wait_s": _rounded(governed.waits) if governed else {},
        "network_s": _rounded(governed.network) if governed else {},
        "duration_s": round(finished - started, 3),
        "cost": cost,
        "reservation_estimator": config.reservation_estimator,
        "smoke_test": bool(args.first_call_only),
        "first_call_accepted": (record.stop_reason == ACCEPTED) if args.first_call_only else None,
        "sends": governed.sends if governed else 0,
        "outcomes": [vars(o) for o in record.outcomes],
    }
    if quota is not None:
        try:
            held = quota.state()
        except QuotaRefused as refusal:
            held = {"unreadable": str(refusal)}
        payload["daily_quota"] = held | {
            "run_sends": governed.sends if governed else 0,
            "assumptions": QUOTA_ASSUMPTIONS,
        }
    return _new_file(args.json, record.started_at, payload)


def _rounded(seconds: dict[str, float]) -> dict[str, float]:
    return {case: round(value, 3) for case, value in seconds.items()}


def _new_file(path: Path, started_at: datetime, payload: dict) -> Path:
    """The run's own file beside `path`, named by its start time: never an
    earlier file overwritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = f"{path.stem}-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    suffix = path.suffix or ".json"
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    for counter in range(1000):
        target = path.with_name(f"{stem}{f'-{counter}' if counter else ''}{suffix}")
        try:
            with open(target, "x", encoding="utf-8") as out:
                out.write(text)
        except FileExistsError:
            continue
        return target
    raise FileExistsError(f"no free name beside {path}")

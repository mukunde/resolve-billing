"""The service's own engines, measured (specs/009-luna-service, FR-014, and
specs/010-haiku-fallback, FR-012).

The runner sends this configuration through `Governed`; the service sends it
through `BoundedEngine`. This tool measures the second, on the development
cases alone, with the first's safeguards: the guard, the declared accounts,
the accepted first call, the account held for the whole run, and the envelope,
reserved before each send and settled after it.

The request is not touched. The result names itself a service run: it is no
slot of spec 008's campaign, and its comparison takes no part of it.

    python -m dotenv run -- python -m evals.service_run \\
        --data-terms contractual --json evals/results/gpt-6-luna-service.json

The composite runs only with `--primary-disabled`: no client for OpenAI is
built, every dispute reaches Haiku through the failover's own wiring, and the
Anthropic envelope pays. It measures that wiring, not an outage.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.api.deps import FAILOVER, HAIKU
from app.decision.base import (
    AttemptEnd,
    AttemptOutcome,
    AttemptStart,
    EngineRejected,
    EngineTelemetry,
    EngineUnavailable,
)
from app.decision.bounded import BoundedEngine, Bounds
from app.decision.claude import PROMPT_VERSION
from app.decision.failover import (
    DEADLINE,
    POLICY,
    PRIMARY_BOUNDS,
    SECONDARY_BOUNDS,
    FailoverEngine,
)
from app.decision.hosted import Answer, configuration
from app.decision.masking import MASKING_VERSION
from app.decision.verdict import verdict_schema
from evals import accounts, campaign, engines
from evals.first_calls import FirstCalls, FirstCallsRefused, fingerprint
from evals.governed import reservation_cost, settlement
from evals.hosted_run import _new_file
from evals.ledger import LedgerRefused, PrepaidLedger
from evals.limits import RunStopped
from evals.state import AccountBusy, AccountRun

ENGINE = "gpt-6-luna"
RUN_KIND = "service run"
STATEMENT = (
    "one run of the development cases through a service engine: a regression score on "
    "cases that shaped the prompt, not a measure on unseen disputes, and no part of spec "
    "008's comparison"
)

#: Outcomes the vendor refused before processing: nothing was spent, as `Governed` holds.
REFUSED_UNPROCESSED = frozenset(
    {AttemptOutcome.NOT_SENT, AttemptOutcome.RATE_LIMITED, AttemptOutcome.REJECTED}
)
#: Outcomes that carry an answer, and so its usage.
ANSWERED = frozenset({AttemptOutcome.ANSWERED, AttemptOutcome.UNUSABLE, AttemptOutcome.WRONG_MODEL})


FAILOVER_VERSION = f"{FAILOVER}/{POLICY}/{PROMPT_VERSION}"


def failover_record() -> dict:
    """The composite measured: its policy and both legs, each by fingerprint."""
    legs = ((ENGINE, PRIMARY_BOUNDS), (HAIKU, SECONDARY_BOUNDS))
    return {
        "policy": POLICY,
        "deadline_s": DEADLINE,
        "legs": [
            {
                "configuration": name,
                "fingerprint": fingerprint(configuration(name)),
                "bounds": asdict(bounds),
            }
            for name, bounds in legs
        ],
        "disabled": ENGINE,
    }


class DisabledLeg:
    """The failover's first leg, out of the measurement: no engine, no client,
    nothing sent, no attempt, and a cost known to be zero."""

    invoice_references = None

    def __init__(self, config) -> None:
        self._config = config

    async def leg(self, dispute, context, findings, invocation_id, ends_at, first_number=1):
        outage = EngineUnavailable("disabled")
        outage.failover_eligible = True
        outage.invocation_id = invocation_id
        outage.telemetry = EngineTelemetry(
            model_id=self._config.model,
            prompt_version=self._config.prompt_version,
            usage_known=False,
            cost_micros=0,
            invocation_id=invocation_id,
        )
        raise outage

    async def aclose(self) -> None:
        pass


class LedgerRecorder:
    """Attempts against the account's envelope: reserved before the send, and
    a refusal sends nothing; settled after it. A reservation with no known cost
    stays spent. Attempts are kept for the result."""

    def __init__(self, ledger, config, run_id: str, on_send=None) -> None:
        self._ledger = ledger
        self._config = config
        self._run_id = run_id
        self._on_send = on_send
        self._reserved: dict[str, tuple[str, int, float]] = {}
        self.open_reservations: dict[str, float] = {}
        self.cost = 0.0
        self.attempts: list[dict] = []

    def started(self, attempt: AttemptStart) -> None:
        ticket = f"{self._run_id}/{attempt.attempt_id}"
        amount = reservation_cost(attempt.reserved_tokens, self._config)
        self._ledger.reserve(ticket, amount)
        self._reserved[attempt.attempt_id] = (ticket, attempt.reserved_tokens, amount)
        self.open_reservations[ticket] = amount
        self.attempts.append(vars(attempt).copy())
        if self._on_send is not None:
            self._on_send()

    def finished(self, attempt: AttemptEnd) -> None:
        ticket, reserved, _ = self._reserved[attempt.attempt_id]
        held = next(a for a in self.attempts if a["attempt_id"] == attempt.attempt_id)
        held.update(vars(attempt) | {"outcome": attempt.outcome.value})
        if attempt.outcome in REFUSED_UNPROCESSED:
            self._settle(ticket, 0.0)
        elif attempt.outcome in ANSWERED:
            answer = Answer(
                None,
                "stop",
                attempt.input_tokens,
                attempt.output_tokens,
                attempt.returned_model,
                cached_input_tokens=attempt.cached_input_tokens,
                cache_write_tokens=attempt.cache_write_tokens,
                reasoning_tokens=attempt.reasoning_tokens,
            )
            cost, stop = settlement(answer, reserved, self._config)
            if cost is not None:
                self._settle(ticket, cost)
            if stop is not None:
                raise RunStopped(f"{self._config.name}: {stop}")
        if attempt.outcome is AttemptOutcome.WRONG_MODEL:
            # Its cost is kept; its answer never scored, as in `Governed`.
            raise RunStopped(f"{self._config.name}: answered by another model")

    def _settle(self, ticket: str, amount: float) -> None:
        self._ledger.settle(ticket, amount)
        self.open_reservations.pop(ticket, None)
        self.cost += amount


async def run(record: dict, cases: list[dict], recorder: LedgerRecorder, account, bounds) -> None:
    """Run the cases into `record`, which holds what was done whatever stops
    the run. Complete only once the engine closed cleanly too."""
    from app.api.deps import wire
    from app.application.investigate_dispute import DisputeInvestigator
    from app.domain.models import Dispute
    from app.providers.fake.adapter import FakeBillingProvider
    from evals import runner

    # The previous run on this account may still count against the minute.
    if (wait := account.wait()) > 0:
        print(f"waiting {wait:.0f} s for the previous run's quota window", file=sys.stderr)
        await asyncio.sleep(wait)
    if record["engine"] == FAILOVER:
        wrapper = FailoverEngine(
            DisabledLeg(configuration(ENGINE)),
            BoundedEngine(engines.HOSTED_ENGINES[HAIKU](), recorder, bounds=bounds),
            name=FAILOVER,
            version=FAILOVER_VERSION,
        )
    else:
        wrapper = BoundedEngine(engines.HOSTED_ENGINES[ENGINE](), recorder, bounds=bounds)
    provider = FakeBillingProvider()
    investigator = DisputeInvestigator(provider=provider, engine=wire(wrapper, provider))
    try:
        for index, case in enumerate(cases):
            record["stopped_at"] = case["id"]
            record["not_attempted"] = [later["id"] for later in cases[index:]]
            dispute = Dispute(
                id=f"eval_{case['id']}",
                customer_message=case["message"],
                customer_email=case["customer_email"],
                customer_id=case.get("customer_id"),
                invoice_id=case["invoice_id"],
                headers=runner.header_pairs(case.get("headers")),
            )
            result = await investigator.investigate(dispute, now=runner.EVALUATION_DATE)
            record["outcomes"].append(runner._evaluate(case, result))
        record["not_attempted"] = []
        record["stopped_at"] = None
    finally:
        await wrapper.aclose()
    record["complete"] = True


def write(record: dict, recorder: LedgerRecorder, ledger, config, args, started: float) -> Path:
    unknown = recorder.open_reservations
    repo = campaign.git_state()
    try:
        envelope = ledger.state()
    except LedgerRefused as refusal:
        envelope = f"unreadable: {refusal}"
    payload = {
        "engine": record["engine"],
        "engine_version": record["engine_version"],
        "primary_disabled": record["engine"] == FAILOVER,
        "run_kind": RUN_KIND,
        "statement": STATEMENT,
        "cases_file": engines.DEVELOPMENT_SET.name,
        "cases_sha256": engines.DEVELOPMENT_SET_SHA256[:16],
        "ran_at": record["started_at"].isoformat(),
        "duration_s": round(time.monotonic() - started, 3),
        "complete": record["complete"],
        "stop_reason": record["stop_reason"],
        "stopped_at": record["stopped_at"],
        "cases_total": record["total"],
        "cases_run": len(record["outcomes"]),
        "not_attempted": record["not_attempted"],
        "bounds": asdict(record["bounds"]),
        "failover": record["failover"],
        "data_terms": args.data_terms,
        "configuration_fingerprint": fingerprint(config),
        "prompt_version": PROMPT_VERSION,
        "masking_version": MASKING_VERSION,
        "schema_sha256": hashlib.sha256(
            json.dumps(verdict_schema(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16],
        "commit": repo["commit"],
        "clean_tree": repo["clean_tree"],
        "account": accounts.identity(config),
        "cost": {
            "basis": "estimated from the reported usage at list prices, by category",
            "currency": config.currency,
            "settled_amount": round(recorder.cost, 6),
            "unknown_cost_attempts": len(unknown),
            "unknown_cost_reserved": round(sum(unknown.values()), 6),
            "complete": not unknown,
            "source": config.price_source,
            "envelope": envelope,
        },
        "attempts": recorder.attempts,
        "answered_by": {
            attempt["dispute_id"].removeprefix("eval_"): attempt.get("returned_model")
            for attempt in recorder.attempts
            if attempt.get("outcome") == AttemptOutcome.ANSWERED.value
        },
        "outcomes": [vars(outcome) for outcome in record["outcomes"]],
    }
    return _new_file(args.json, record["started_at"], payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--data-terms", required=True)
    parser.add_argument("--cases", type=Path, default=engines.DEVELOPMENT_SET)
    parser.add_argument("--engine", choices=(ENGINE, FAILOVER), default=ENGINE)
    parser.add_argument(
        "--primary-disabled",
        action="store_true",
        help="the composite only: measure its wiring with Luna's leg out",
    )
    args = parser.parse_args(argv)
    if (args.engine == FAILOVER) != args.primary_disabled:
        parser.error(f"{FAILOVER} runs with --primary-disabled, and nothing else does")

    config = configuration(HAIKU if args.engine == FAILOVER else ENGINE)
    try:
        cases = engines.development_cases(args.cases)
        FirstCalls().admit_full_run(config)
        ledger = PrepaidLedger(accounts.billing_account(config), config.currency)
        ledger.state()
    except (
        engines.OffTheDevelopmentSet,
        FirstCallsRefused,
        accounts.AccountRefused,
        LedgerRefused,
    ) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2

    bounds = SECONDARY_BOUNDS if args.engine == FAILOVER else Bounds()
    began = datetime.now(UTC)
    started = time.monotonic()
    record = {
        "engine": args.engine,
        "engine_version": FAILOVER_VERSION if args.engine == FAILOVER else config.prompt_version,
        "failover": failover_record() if args.engine == FAILOVER else None,
        "started_at": began,
        "total": len(cases),
        "outcomes": [],
        "not_attempted": [case["id"] for case in cases],
        "stopped_at": None,
        "complete": False,
        "stop_reason": None,
        "bounds": bounds,
    }
    try:
        with AccountRun(accounts.billing_account(config)) as account:
            recorder = LedgerRecorder(
                ledger, config, began.strftime("%Y%m%dT%H%M%S.%fZ"), account.record_send
            )
            try:
                asyncio.run(run(record, cases, recorder, account, bounds))
            except RunStopped as stop:
                record["stop_reason"] = str(stop)
            except LedgerRefused as refusal:
                record["stop_reason"] = f"the ledger refused: {refusal}"
            except EngineRejected as refusal:
                record["stop_reason"] = f"the engine refused the request ({refusal})"
            except (KeyboardInterrupt, asyncio.CancelledError):
                record["stop_reason"] = "interrupted"
            except Exception as error:
                record["stop_reason"] = f"internal error ({type(error).__name__})"
                written = write(record, recorder, ledger, config, args, started)
                print(f"result written to {written}", file=sys.stderr)
                raise
            written = write(record, recorder, ledger, config, args, started)
    except AccountBusy as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2

    from evals import runner

    if not record["complete"]:
        print(f"INCOMPLETE: {record['stop_reason']}", file=sys.stderr)
    print(f"\n{STATEMENT}")
    status = runner.report(args.engine, record["outcomes"])
    print(f"\nresult written to {written}")
    return status if record["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

"""The Claude engine's request and decisions, pinned before the verdict
helpers moved out of it (specs/004-second-engines, T020).

The snapshot was recorded from the engine as it stood before the move; the
move must leave every byte of the request and every field of the decisions
as they were. The mock baseline does not cover Claude, so this does.
"""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json
from pathlib import Path

from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    engine_with,
    findings,
    reply,
)

SNAPSHOT = Path(__file__).parent / "snapshots" / "claude_before_verdict_move.json"

OUTCOMES = {
    "valid": reply(VALID_VERDICT),
    "invalid": reply("{not json"),
    "truncated": reply(VALID_VERDICT, stop_reason="max_tokens"),
}


async def observed(context, findings, dispute) -> dict:
    seen = {}
    for name, outcome in OUTCOMES.items():
        engine, client = engine_with(outcome)
        decision = await engine.decide(dispute, context, findings)
        record = decision.model_dump(mode="json")
        record["telemetry"].pop("latency_ms")
        seen[name] = {"request": client.messages.last_request, "decision": record}
    return seen


async def test_the_request_and_the_decisions_are_as_they_were(context, findings, dispute):
    seen = json.loads(json.dumps(await observed(context, findings, dispute), ensure_ascii=False))
    assert seen == json.loads(SNAPSHOT.read_text(encoding="utf-8"))

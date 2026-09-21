# ADR-006 — No agent framework

**Status:** accepted · 2026-09-21

## Context

The 2026 landscape has settled into three families: provider-native SDKs
(Claude Agent SDK, OpenAI Agents SDK, AWS Strands), graph orchestrators with
checkpoints (LangGraph), and light typed frameworks (Pydantic AI).

A repository that uses none of them invites one of two readings: the author
did not know, or the author decided. This record makes it the second.

## Decision

No agent framework. Two `Protocol` types, the provider's Messages API, and
n8n for triggering and notification.

The justification is not that the flow is small. It is that **the flow
contains no agent loop**:

- no path routes a model's output back into a model;
- the billing context is loaded once, in full, before the call — the model
  holds no tools and cannot fetch anything;
- `classification` takes its values in a **closed, enumerated** set, routing is
  a deterministic function of that value, and anything outside it goes to a
  human.

The model supplies a switch value. It never chooses its next action, nor a
tool, nor a branch. That is a fixed graph with model calls at fixed positions,
not an agent — and a graph orchestrator would add no node to it.

**The count of model calls is the wrong invariant.** It goes from one to two
the moment a reply drafter is added. The invariant that holds is the absence
of a loop and the absence of control flow decided by the model.

The decisive argument for a sovereignty-sensitive context: **a framework
dependency is a second lock beside the model.** The whole value of the
`DecisionEngine` port evaporates if the orchestration layer is captive.

## Consequences

What a framework would have supplied, and where it comes from instead:
suspension and resumption for human approval, provided by the state machine in
the database. That has an advantage a checkpointer does not — it is queryable
in SQL and legible to a financial controller.

**Re-evaluation condition, stated so it can be checked rather than argued:**
if the flow exceeds roughly five nodes with mid-run resumption, or a
refinement loop appears, move to a checkpointed orchestrator. Both ports are
built to survive that change, and the roadmap carries the item with this
condition attached rather than as an aspiration.

The cost of the decision is real: everything a framework would have given is
written here — the retry policy, the idempotency key, the state machine, the
audit schema. That is about a thousand lines that someone else maintains in
the alternative.

**Not to repeat in conversation:** the widely circulated claim that Claude Code
or OpenAI's deep-research products run on LangGraph is false. The Claude Agent
SDK is Anthropic's own harness, extracted from Claude Code.

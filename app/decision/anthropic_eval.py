"""`claude-haiku-5-5` on the governed evaluation path
(specs/008-deployment-model-choice).

The service runs `ClaudeDecisionEngine`, and that is what a choice of Claude
would deploy. This adapter measures it under the discipline the other
vendors' configurations already have -- the envelope, the first call, a
status per case, a result written even when a run stops -- without changing
the service engine: it sends the request the service engine sends (same
model, output cap, system prompt, effort and schema, the same user payload)
and reads the answer as the service engine's parser does, `refusal` and
`max_tokens` sent to a person, the text validated for every other stop
reason. Tests hold the two engines' requests and decisions equal.

What differs is how outages are retried: the SDK's own retries are off here,
and the runner counts attempts, as for the other vendors. `anthropic` pinned
at 0.105.2; the key and the address are passed, two settings the SDK would
read from the environment -- extra headers (`ANTHROPIC_CUSTOM_HEADERS`) and
request logging (`ANTHROPIC_LOG`) -- are refused, and responses are
validated strictly. Imported by the evaluation runner only.
"""

from __future__ import annotations

import json
import os

from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.claude import _TRANSIENT, _response_schema
from app.decision.hosted import (
    Answer,
    Configuration,
    HostedEngine,
    estimated_tokens,
    silence_request_logging,
    unavailable,
)

#: A margin over the request's UTF-8 bytes for the message framing and the
#: schema; an estimate, checked against each answer's reported usage.
TEMPLATE_MARGIN = 512

#: Settings the SDK reads from the environment that would change every
#: request: refused while set.
AMBIENT = ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG")

#: The two stop reasons the service engine's parser sends to a person.
_UNUSABLE = {"max_tokens": "length", "refusal": "filtered"}


class AmbientSettingRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """A client cannot be built as the environment stands."""


def ambient_refusal() -> str | None:
    """Why no client may be built, or None."""
    present = [name for name in AMBIENT if os.environ.get(name)]
    if present:
        return (
            f"{', '.join(present)} set: the SDK would apply it to every request; "
            "unset it to run this engine"
        )
    return None


class AnthropicEvaluationEngine(HostedEngine):
    def __init__(self, config: Configuration, client=None, invoice_references=None) -> None:
        super().__init__(config, invoice_references)
        self._client = client if client is not None else build_client(config)

    async def _complete(self, system: str, user: str) -> Answer:
        try:
            response = await self._client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_output_tokens,
                system=system,
                output_config={"effort": self.config.reasoning, "format": _response_schema()},
                messages=[{"role": "user", "content": user}],
            )
        except Exception as error:
            if _unreadable(error):
                # The vendor answered -- possibly billed -- in a shape the SDK
                # could not read: unusable, its usage unknown.
                return Answer(None, "other", None, None, None, ("response_unreadable",))
            translated = _translated(error)
            if translated is None:
                raise
            raise translated from None
        return _answer(response)

    async def reservation(self, system: str, user: str) -> int:
        """The request's UTF-8 bytes, plus the framing margin, plus the
        output cap."""
        return estimated_tokens(system, user, TEMPLATE_MARGIN, self.config.max_output_tokens)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _answer(response) -> Answer:
    usage = getattr(response, "usage", None)
    input_tokens = output_tokens = None
    cached = written = reasoning = None
    if usage is not None:
        reported_in = getattr(usage, "input_tokens", None)
        reported_out = getattr(usage, "output_tokens", None)
        cached = getattr(usage, "cache_read_input_tokens", None)
        written = getattr(usage, "cache_creation_input_tokens", None)
        if reported_in is not None and reported_out is not None:
            # Anthropic counts cache reads and writes apart from
            # `input_tokens`; an Answer's input total includes them.
            input_tokens = reported_in + (cached or 0) + (written or 0)
            output_tokens = reported_out
        details = getattr(usage, "output_tokens_details", None)
        reasoning = getattr(details, "thinking_tokens", None) if details is not None else None
    version = getattr(response, "model", None)
    details = {
        "cached_input_tokens": cached,
        "cache_write_tokens": written,
        "reasoning_tokens": reasoning,
    }
    stop = str(getattr(response, "stop_reason", ""))
    if stop in _UNUSABLE:
        return Answer(None, _UNUSABLE[stop], input_tokens, output_tokens, version, **details)
    text = next(
        (
            block.text
            for block in getattr(response, "content", None) or []
            if getattr(block, "type", None) == "text"
        ),
        None,
    )
    return Answer(text or None, "stop", input_tokens, output_tokens, version, **details)


def _translated(error: Exception) -> Exception | None:
    """The port's error for the SDK's, by category, never the vendor's text;
    None for anything else -- a defect, left to fail as itself."""
    import anthropic

    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        if status == 429:
            return unavailable("rate_limited", error.response)
        if status not in _TRANSIENT and 400 <= status < 500:
            # A refusal: the request was not processed, nothing was spent.
            return EngineRejected(str(status))
        # An outage (408, 409, 5xx) or an unexpected status: the outcome is
        # unknown, and the reservation stays spent.
        return unavailable("server_error", error.response)
    if isinstance(error, anthropic.APIConnectionError):
        return EngineUnavailable("transport")
    return None


def _unreadable(error: Exception) -> bool:
    """A response the SDK could not read: not of its types (strict
    validation), or not JSON at all where JSON was announced."""
    import anthropic

    return isinstance(error, anthropic.APIResponseValidationError | json.JSONDecodeError)


def build_client(config: Configuration, http_client=None):
    """The SDK's client for this configuration: every setting passed, none
    left to the environment."""
    from anthropic import AsyncAnthropic

    if (refusal := ambient_refusal()) is not None:
        raise AmbientSettingRefused(refusal)
    missing = [name for name in config.account_variables if not os.environ.get(name)]
    if missing:
        raise AmbientSettingRefused(f"{', '.join(missing)} not set: the account is not named")
    # The SDK logs each request's options, payload included, at DEBUG.
    silence_request_logging("anthropic._base_client")
    settings = {
        "api_key": os.environ[config.key_variable],
        "base_url": config.endpoint,
        "max_retries": 0,
        "_strict_response_validation": True,
        "default_headers": {"anthropic-version": config.api_version},
    }
    if http_client is not None:
        settings["http_client"] = http_client
    return AsyncAnthropic(**settings)

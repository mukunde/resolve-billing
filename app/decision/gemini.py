"""`gemini-3.8-flash`, through Google's Gemini API (specs/004-second-engines).

The native API, as Google recommends for a new integration, through
`google-genai` pinned at 2.29.0: `generate_content` with the verdict schema as
structured JSON output, thinking at its lowest level for this model (`low`;
it cannot be turned off), the output cap -- which includes thinking tokens --
at 4096, and the SDK's own retries off: the runner counts attempts. Imported
by the evaluation runner only.
"""

from __future__ import annotations

import json
import os

from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.hosted import Answer, Configuration, HostedEngine, unavailable
from app.decision.verdict import verdict_schema

#: A margin over the counted text for the request's framing -- the system
#: instruction and the schema sent as fields, not text. An estimate (the
#: maintainer's waiver, 2026-10-24), checked against the prompt tokens each
#: answer reports.
FRAMING_MARGIN = 256

#: How each finish reason reads: anything but a complete stop is unusable.
_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"}
_FILTERED = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "LANGUAGE"}


class GeminiEngine(HostedEngine):
    def __init__(self, config: Configuration, client=None, invoice_references=None) -> None:
        super().__init__(config, invoice_references)
        self._client = client if client is not None else _default_client(config)

    def _config(self, system: str):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=verdict_schema(),
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self.config.reasoning.upper())
            ),
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
        )

    async def _complete(self, system: str, user: str) -> Answer:
        try:
            response = await self._client.aio.models.generate_content(
                model=self.config.model, contents=user, config=self._config(system)
            )
        except Exception as error:
            translated = _translated(error)
            if translated is None:
                raise
            raise translated from None
        return _answer(response)

    async def reservation(self, system: str, user: str) -> int:
        """Counted by Google on one text holding everything the request
        carries, plus the framing margin, plus the output cap."""
        text = "\n".join((system, user, json.dumps(verdict_schema())))
        try:
            counted = await self._client.aio.models.count_tokens(
                model=self.config.model, contents=text, config=_count_config()
            )
        except Exception as error:
            translated = _translated(error)
            if translated is None:
                raise
            raise translated from None
        return counted.total_tokens + FRAMING_MARGIN + self.config.max_output_tokens


def _answer(response) -> Answer:
    candidates = getattr(response, "candidates", None) or []
    input_tokens, output_tokens = _usage(getattr(response, "usage_metadata", None))
    version = getattr(response, "model_version", None)
    if not candidates:
        return Answer(None, "filtered", input_tokens, output_tokens, version)
    candidate = candidates[0]
    reason = getattr(candidate.finish_reason, "value", candidate.finish_reason) or ""
    finish = _FINISH.get(reason, "filtered" if reason in _FILTERED else "other")
    parts = (candidate.content.parts if candidate.content else None) or []
    # Thoughts are never the answer, and never kept.
    text = "".join(p.text for p in parts if p.text and not p.thought) or None
    return Answer(text, finish, input_tokens, output_tokens, version)


def _usage(usage) -> tuple[int | None, int | None]:
    """Input and output tokens as reported, or None for what was not.

    Output is the total less the prompt when the total is reported -- it
    then holds thinking too; otherwise the candidates' count, which must be
    there, plus thinking. A count never reported is never taken as zero.
    """
    if usage is None or usage.prompt_token_count is None:
        return None, None
    prompt = usage.prompt_token_count
    if usage.total_token_count is not None:
        return prompt, usage.total_token_count - prompt
    if usage.candidates_token_count is None:
        return prompt, None
    return prompt, usage.candidates_token_count + (usage.thoughts_token_count or 0)


def _transport_errors() -> tuple[type[BaseException], ...]:
    import httpx

    errors: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, httpx.TransportError)
    try:
        import aiohttp
    except ImportError:
        return errors
    return (*errors, aiohttp.ClientError)


def _count_config():
    from google.genai import types

    return types.CountTokensConfig(
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
    )


def _translated(error: Exception) -> Exception | None:
    """The port's error for Google's, by category: never Google's text;
    None for anything neither Google's nor the network's -- a defect, left
    to fail as itself."""
    from google.genai import errors

    if isinstance(error, errors.APIError):
        code = getattr(error, "code", None)
        response = getattr(error, "response", None)
        if code == 429:
            return unavailable("rate_limited", response)
        if code is not None and code >= 500:
            return unavailable("server_error", response)
        return EngineRejected(str(code))
    if isinstance(error, _transport_errors()):
        return EngineUnavailable("transport")
    return None


def _default_client(config: Configuration):
    """The Gemini API itself, at the pinned address and version: never Vertex
    AI, never an address the environment names."""
    from google import genai
    from google.genai import types

    return genai.Client(
        vertexai=False,
        api_key=os.environ[config.key_variable],
        http_options=types.HttpOptions(base_url=config.endpoint, api_version=config.api_version),
    )

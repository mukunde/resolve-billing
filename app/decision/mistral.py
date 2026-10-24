"""`mistral-large-4` and `zai-glm-5-3`, through Mistral's API
(specs/004-second-engines).

`mistralai` pinned at 3.2.0: one chat completion with the verdict schema as a
strict JSON schema, the reasoning effort the configuration pins (`none` for
Large 4; `low` for GLM 5.3, which cannot turn it off), the output cap at
4096, and the SDK's retries off (`retries=None`). GLM 5.3 is Z.ai's model,
served by Mistral on its own infrastructure. The content may come as chunks:
the final text chunk is the answer, thinking chunks are dropped and never
kept. Imported by the evaluation runner only.
"""

from __future__ import annotations

import json
import os

from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.hosted import Answer, Configuration, HostedEngine, unavailable
from app.decision.verdict import verdict_schema

#: A margin over the request's UTF-8 bytes for the chat template's special
#: tokens and the schema's framing. An estimate: neither the tokenizer of the
#: served models nor these overheads are documented (the maintainer's waiver,
#: 2026-10-24); the prompt tokens each answer reports are checked against it.
TEMPLATE_MARGIN = 512

_FINISH = {"stop": "stop", "length": "length", "model_length": "length"}


class MistralEngine(HostedEngine):
    def __init__(self, config: Configuration, client=None, invoice_references=None) -> None:
        super().__init__(config, invoice_references)
        self._client = client if client is not None else _default_client(config)

    async def _complete(self, system: str, user: str) -> Answer:
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": verdict_schema(), "strict": True},
            },
            "reasoning_effort": self.config.reasoning,
            # None disables the SDK's retries; omitting it inherits the client's.
            "retries": None,
        }
        if self.config.temperature is not None:
            request["temperature"] = self.config.temperature
        try:
            response = await self._client.chat.complete_async(**request)
        except Exception as error:
            translated = _translated(error)
            if translated is None:
                raise
            raise translated from None
        return _answer(response)

    async def reservation(self, system: str, user: str) -> int:
        """The request's UTF-8 bytes -- a byte-level tokenizer gives each
        ordinary token at least one -- plus the template margin, plus the
        output cap."""
        text = system + user + json.dumps(verdict_schema())
        return len(text.encode("utf-8")) + TEMPLATE_MARGIN + self.config.max_output_tokens


def _answer(response) -> Answer:
    usage = getattr(response, "usage", None)
    input_tokens = output_tokens = None
    # The SDK fills an absent count with 0: presence is read from the fields
    # the response actually set, never from the values.
    present = getattr(usage, "model_fields_set", set()) if usage is not None else set()
    if {"prompt_tokens", "completion_tokens"} <= present:
        input_tokens, output_tokens = usage.prompt_tokens, usage.completion_tokens
    version = getattr(response, "model", None)
    choices = getattr(response, "choices", None) or []
    if not choices:
        return Answer(None, "other", input_tokens, output_tokens, version)
    choice = choices[0]
    finish = _FINISH.get(str(choice.finish_reason), "other")
    return Answer(_final_text(choice.message.content), finish, input_tokens, output_tokens, version)


def _final_text(content) -> str | None:
    """A string, or the last text chunk of a chunk list; thinking dropped."""
    if isinstance(content, str):
        return content or None
    texts = [
        getattr(chunk, "text", None)
        for chunk in content or []
        if getattr(chunk, "type", None) == "text"
    ]
    texts = [text for text in texts if text]
    return texts[-1] if texts else None


def _translated(error: Exception) -> Exception | None:
    """The port's error for Mistral's, by category: never Mistral's text;
    None for anything neither Mistral's nor the network's -- a defect, left
    to fail as itself."""
    import httpx2
    from mistralai.client import errors

    if isinstance(error, errors.NoResponseError):
        return EngineUnavailable("transport")
    status = getattr(error, "status_code", None)
    if isinstance(error, errors.MistralError) and status is not None:
        response = getattr(error, "raw_response", None)
        if status == 429:
            return unavailable("rate_limited", response)
        if status >= 500:
            return unavailable("server_error", response)
        return EngineRejected(str(status))
    if isinstance(error, TimeoutError | ConnectionError | httpx2.TransportError):
        return EngineUnavailable("transport")
    return None


def _default_client(config: Configuration):
    from mistralai.client import Mistral

    return Mistral(api_key=os.environ[config.key_variable], server_url=config.endpoint)

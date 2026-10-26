"""`gpt-6-luna` and `gpt-6.1-sol` through OpenAI's API, and
`qwen3.5-397b-a17b` through Scaleway's OpenAI-compatible API
(specs/007-openai-qwen-engines).

`openai` pinned at 3.29.0, for both: one chat completion with the verdict
schema in `response_format`, the reasoning effort the configuration pins
(`low`), the output cap as `max_completion_tokens` (reasoning included, for
OpenAI), and the SDK's retries off. What differs by vendor stays here:
OpenAI's request carries `strict` and `store: false` and the declared
organisation and project; Scaleway's carries neither (`store` is not
supported there), its project goes in the address, and OpenAI's account
headers are removed so they never reach it.

The SDK reads some settings from the environment when the client does not
pass them. The address, the key and the account are always passed; two
settings that would change every request -- extra headers
(`OPENAI_CUSTOM_HEADERS`) and request logging (`OPENAI_LOG`) -- are refused
before any client is built. Responses are validated strictly: a body that
does not match the SDK's types is an unreadable answer, never a verdict.
Reasoning text (Scaleway's `reasoning_content`) and a refusal's text are
never read. Imported by the evaluation runner only.
"""

from __future__ import annotations

import os

from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.hosted import (
    Answer,
    Configuration,
    HostedEngine,
    estimated_tokens,
    unavailable,
)
from app.decision.verdict import verdict_schema

#: A margin over the request's UTF-8 bytes for the chat template and the
#: schema's framing. An estimate, as for Mistral (the maintainer's waiver,
#: extended to these vendors with spec 007): each answer's reported usage
#: is checked against it.
TEMPLATE_MARGIN = 512

#: Settings the SDK reads from the environment that would change every
#: request: refused while set.
AMBIENT = ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG")

_FINISH = {"stop": "stop", "length": "length", "content_filter": "filtered"}


class AmbientSettingRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """A client cannot be built as the environment stands."""


def ambient_refusal() -> str | None:
    """Why no client may be built, or None."""
    present = [name for name in AMBIENT if os.environ.get(name)]
    if present:
        return (
            f"{', '.join(present)} set: the SDK would apply it to every request; "
            "unset it to run these engines"
        )
    return None


class OpenAICompatibleEngine(HostedEngine):
    def __init__(self, config: Configuration, client=None, invoice_references=None) -> None:
        super().__init__(config, invoice_references)
        self._client = client if client is not None else build_client(config)

    async def _complete(self, system: str, user: str) -> Answer:
        schema = {"name": "verdict", "schema": verdict_schema()}
        if self.config.vendor == "openai":
            schema["strict"] = True
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": self.config.max_output_tokens,
            "reasoning_effort": self.config.reasoning,
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
        if self.config.store is not None:
            request["store"] = self.config.store
        if self.config.temperature is not None:
            request["temperature"] = self.config.temperature
        try:
            response = await self._client.chat.completions.create(**request)
        except Exception as error:
            if _unreadable(error):
                # The vendor answered -- possibly generated, possibly billed --
                # in a shape the SDK could not read: an unusable answer whose
                # usage is unknown, never a refusal that costs nothing.
                return Answer(None, "other", None, None, None, ("response_unreadable",))
            translated = _translated(error)
            if translated is None:
                raise
            raise translated from None
        return _answer(response)

    async def reservation(self, system: str, user: str) -> int:
        """The request's UTF-8 bytes, plus the template margin, plus the
        output cap."""
        return estimated_tokens(system, user, TEMPLATE_MARGIN, self.config.max_output_tokens)

    async def aclose(self) -> None:
        await self._client.close()


def _answer(response) -> Answer:
    usage = getattr(response, "usage", None)
    input_tokens = output_tokens = None
    cached = written = reasoning = None
    if usage is not None:
        # Presence is read from the fields the response set, never from a
        # value the SDK may have defaulted.
        if {"prompt_tokens", "completion_tokens"} <= usage.model_fields_set:
            input_tokens, output_tokens = usage.prompt_tokens, usage.completion_tokens
        cached = _detail(usage, "prompt_tokens_details", "cached_tokens")
        written = _detail(usage, "prompt_tokens_details", "cache_write_tokens")
        reasoning = _detail(usage, "completion_tokens_details", "reasoning_tokens")
    version = getattr(response, "model", None)
    choices = getattr(response, "choices", None) or []
    if not choices:
        return Answer(None, "other", input_tokens, output_tokens, version)
    choice = choices[0]
    message = choice.message
    details = {
        "cached_input_tokens": cached,
        "cache_write_tokens": written,
        "reasoning_tokens": reasoning,
    }
    if getattr(message, "refusal", None):
        # The model declined: unusable, and its words are not kept.
        return Answer(None, "filtered", input_tokens, output_tokens, version, **details)
    finish = _FINISH.get(str(choice.finish_reason), "other")
    text = message.content or None
    return Answer(text, finish, input_tokens, output_tokens, version, **details)


def _detail(usage, group: str, field: str) -> int | None:
    details = getattr(usage, group, None)
    if details is None or field not in details.model_fields_set:
        return None
    return getattr(details, field)


def _translated(error: Exception) -> Exception | None:
    """The port's error for the SDK's, by category: never the vendor's text;
    None for anything else -- a defect, left to fail as itself."""
    import openai

    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        if status == 429:
            return unavailable("rate_limited", error.response)
        if 400 <= status < 500:
            return EngineRejected(str(status))
        return unavailable("server_error", error.response)
    if isinstance(error, openai.APIConnectionError):
        return EngineUnavailable("transport")
    return None


def _unreadable(error: Exception) -> bool:
    import openai

    return isinstance(error, openai.APIResponseValidationError)


def build_client(config: Configuration, http_client=None):
    """The SDK's client for this configuration: every setting passed, none
    left to the environment."""
    from openai import AsyncOpenAI, Omit

    if (refusal := ambient_refusal()) is not None:
        raise AmbientSettingRefused(refusal)
    missing = [name for name in config.account_variables if not os.environ.get(name)]
    if missing:
        raise AmbientSettingRefused(f"{', '.join(missing)} not set: the account is not named")
    settings = {
        "api_key": os.environ[config.key_variable],
        "max_retries": 0,
        "_strict_response_validation": True,
    }
    if http_client is not None:
        settings["http_client"] = http_client
    if config.vendor == "openai":
        organisation, project = config.account_variables
        return AsyncOpenAI(
            base_url=config.endpoint,
            organization=os.environ[organisation],
            project=os.environ[project],
            **settings,
        )
    if config.vendor == "scaleway":
        _, project = config.account_variables
        return AsyncOpenAI(
            base_url=config.endpoint.format(project=os.environ[project]),
            # Never OpenAI's account headers, whatever the environment holds.
            default_headers={"OpenAI-Organization": Omit(), "OpenAI-Project": Omit()},
            **settings,
        )
    raise KeyError(config.vendor)

"""Builds the client of an engine evaluated outside the service, by its vendor
(specs/004-second-engines, specs/007-openai-qwen-engines). Used by the
evaluation registry alone."""

from __future__ import annotations

from app.decision.hosted import Configuration, HostedEngine


def client_for(config: Configuration) -> HostedEngine:
    if config.vendor == "google":
        from app.decision.gemini import GeminiEngine

        return GeminiEngine(config)
    if config.vendor == "mistral":
        from app.decision.mistral import MistralEngine

        return MistralEngine(config)
    if config.vendor == "anthropic":
        from app.decision.anthropic_eval import AnthropicEvaluationEngine

        return AnthropicEvaluationEngine(config)
    if config.vendor in {"openai", "scaleway"}:
        from app.decision.openai_compatible import OpenAICompatibleEngine

        return OpenAICompatibleEngine(config)
    raise KeyError(config.vendor)

"""Builds the client of an engine evaluated on a free tier, by its vendor
(specs/004-second-engines). Used by the evaluation registry alone."""

from __future__ import annotations

from app.decision.hosted import Configuration, HostedEngine


def client_for(config: Configuration) -> HostedEngine:
    if config.vendor == "google":
        from app.decision.gemini import GeminiEngine

        return GeminiEngine(config)
    if config.vendor == "mistral":
        from app.decision.mistral import MistralEngine

        return MistralEngine(config)
    raise KeyError(config.vendor)

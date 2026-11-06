"""The decision prompts, by version (specs/012-prompt-v4-usage, FR-005).

A changed prompt is a new version, never an edited file. The service sends
`DEFAULT_PROMPT` and nothing else; a measurement chooses another explicitly,
and records which. Moving the default is the maintainer's decision, taken on a
measurement.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent / "prompts"

#: Every prompt an engine may send, by version.
PROMPTS: dict[str, Path] = {
    "decision_v3": _DIR / "decision_v3.md",
    "decision_v4": _DIR / "decision_v4.md",
}

#: What the service sends.
DEFAULT_PROMPT = "decision_v3"


class UnknownPrompt(ValueError):
    """A prompt version that does not exist."""


def prompt_path(version: str) -> Path:
    try:
        return PROMPTS[version]
    except KeyError:
        raise UnknownPrompt(f"no prompt {version!r}; known: {sorted(PROMPTS)}") from None


def prompt_text(version: str) -> str:
    return prompt_path(version).read_text(encoding="utf-8")

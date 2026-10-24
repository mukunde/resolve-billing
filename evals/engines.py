"""The engines the evaluation runner may use, and where it may use them.

The service's engines (`app/api/deps.py`, `ENGINES`) plus the three
configurations evaluated on free tiers (specs/004-second-engines):
`gemini-3.8-flash`, `mistral-large-4`, `zai-glm-5-3`. Those three are not
the service's to run, and they run here on the development cases only: the
runner refuses them on any other case file, or with extra fixtures, before
it reads a case or builds a client.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from app.api import deps
from app.decision.hosted import CONFIGURATIONS
from app.providers.fake.fixtures import EXTRA_FIXTURES_ENV

#: The development set, and the hash of its committed bytes (line endings
#: normalised to LF, as git stores them). Changing the cases is a deliberate
#: update of this constant; a test holds them together.
DEVELOPMENT_SET = Path(__file__).parent / "cases.json"
DEVELOPMENT_SET_SHA256 = "4dc2740be58d04b73bde24d903ab76f5997f9701157ab7c0d64a9a31c71e9c99"

#: The engines evaluated on free tiers.
HOSTED = frozenset(CONFIGURATIONS)

#: Engines that cannot run without a credential, and the variables that can
#: carry it. Checked before the first case rather than discovered on it.
CREDENTIALS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    **{name: (config.key_variable,) for name, config in CONFIGURATIONS.items()},
}


def _hosted(name: str) -> Callable[[], object]:
    def build():
        from app.decision.vendors import client_for

        return client_for(CONFIGURATIONS[name])

    return build


#: How each engine evaluated on a free tier is built.
HOSTED_ENGINES: dict[str, Callable[[], object]] = {name: _hosted(name) for name in HOSTED}


def evaluation_engines() -> dict[str, Callable[[], object]]:
    """Every engine the runner may build, by name: the service's table, read
    when asked, and the three."""
    return {**deps.ENGINES, **HOSTED_ENGINES}


class OffTheDevelopmentSet(Exception):  # noqa: N818 -- a refusal, not a defect
    """An engine evaluated on a free tier was asked to run on anything but
    the development set."""


def guard(engine_name: str, cases_path: Path) -> list[dict] | None:
    """For the three, the development cases -- read once, checked, parsed
    from those same bytes; for any other engine, None (no restriction)."""
    if engine_name not in HOSTED:
        return None
    return development_cases(cases_path)


def development_cases(cases_path: Path) -> list[dict]:
    """The development cases, or a refusal: extra fixtures configured, a path
    other than the development set's own (links followed), or bytes that are
    not the committed ones."""
    if os.getenv(EXTRA_FIXTURES_ENV):
        raise OffTheDevelopmentSet(
            f"extra fixtures are configured ({EXTRA_FIXTURES_ENV}): these engines run on "
            "the development set alone"
        )
    if DEVELOPMENT_SET.is_symlink() or cases_path.resolve() != DEVELOPMENT_SET.resolve():
        raise OffTheDevelopmentSet(
            f"{cases_path} is not the development set ({DEVELOPMENT_SET.name}): these engines "
            "run on it alone"
        )
    data = DEVELOPMENT_SET.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != DEVELOPMENT_SET_SHA256:
        raise OffTheDevelopmentSet(
            "the development set's bytes are not the committed ones (altered, or the "
            "recorded hash not updated)"
        )
    return json.loads(data)

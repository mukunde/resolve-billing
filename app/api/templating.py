"""Rendering for the two HTML pages this API serves.

Everything that came from data is escaped, without the caller having to
remember. A page that moves money, on a system whose threat model opens with
"the dispute message is untrusted", is the last place to hand-roll that.

The one way past it is :class:`Safe`, and it is a class rather than a flag so
that finding every unescaped insertion in the codebase is one grep for
``Safe(``. A caller reaching for it is asserting that they escaped the parts
that came from data themselves -- which is why the helpers that build markup
take already-escaped pieces rather than raw rows.
"""

from __future__ import annotations

import html
from pathlib import Path
from string import Template

TEMPLATES = Path(__file__).parent / "templates"


class Safe(str):
    """Markup the caller has already escaped. Inserted verbatim."""


def render(name: str, **fields) -> str:
    """Fill a template, escaping every value that is not explicitly Safe."""
    template = Template((TEMPLATES / name).read_text(encoding="utf-8"))
    return template.safe_substitute(
        {
            key: value if isinstance(value, Safe) else html.escape(str(value), quote=True)
            for key, value in fields.items()
        }
    )


def escape(value) -> str:
    """Escape one value for insertion into markup a caller is building."""
    return html.escape(str(value), quote=True)

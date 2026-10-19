"""Is this message automated? See ADR-011.

A deterministic check on mail headers and the sender address (ADR-003). It
answers "must this never get an automatic reply?", not "is this spam?": a
list header or a bulk precedence says software sent the message, nothing
about whether it is wanted.

Headers are untrusted input, and the design holds anyway: a finding here can
only take an automatic reply away. None grants one, calls the engine, or
widens what the policy allows.

Two findings, because they call for different handling:

- ``AUTOMATED_RESPONSE`` -- an auto-reply or a delivery report. It answers
  something we or someone else sent; there is nothing to investigate.
- ``AUTOMATED_MESSAGE`` -- generated, bulk or list mail, or a sender asking
  for no reply. A real invoice can come from software, so a known sender's
  message is still investigated; it is only never answered automatically.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.enums import RuleCode, Severity
from app.domain.models import RuleFinding

#: `X-Auto-Response-Suppress` values asking responders to stay quiet.
_SUPPRESS = frozenset({"all", "autoreply", "oof", "dr", "ndr", "rn", "nrn"})

#: Precedence values that mark bulk or list mail.
_BULK = frozenset({"bulk", "junk", "list"})

#: Local parts that send delivery reports.
_REPORTERS = frozenset({"mailer-daemon", "postmaster"})

#: Longest value token kept as evidence: values are untrusted text.
_TOKEN = 40

#: Bounds on forwarded headers, shared by every intake. Generous for real
#: mail, and they keep an untrusted field from becoming a second body.
MAX_HEADER_NAMES = 100
MAX_HEADER_VALUES = 10
MAX_HEADER_LENGTH = 1_000


def header_problem(headers: object) -> str | None:
    """Why `headers` -- a name to a string or a non-empty list of strings --
    is not acceptable, or None. An empty list is refused rather than read as
    absent: it would erase a header that says the mail is automated."""
    if not isinstance(headers, dict):
        return "headers must map a name to a value"
    if len(headers) > MAX_HEADER_NAMES:
        return f"at most {MAX_HEADER_NAMES} headers"
    for name, value in headers.items():
        values = value if isinstance(value, list) else [value]
        if not isinstance(name, str) or not all(isinstance(v, str) for v in values):
            return "header names and values must be strings"
        if not values:
            return "a header needs at least one value"
        if len(values) > MAX_HEADER_VALUES:
            return f"at most {MAX_HEADER_VALUES} values per header"
        if len(name) > MAX_HEADER_LENGTH or any(len(v) > MAX_HEADER_LENGTH for v in values):
            return f"header names and values are at most {MAX_HEADER_LENGTH} long"
    return None


def automated_message(
    sender_email: str | None, headers: Iterable[tuple[str, str]]
) -> RuleFinding | None:
    """A finding when the message is automated, or None."""
    values: dict[str, list[str]] = {}
    for name, value in headers:
        values.setdefault(name.strip().lower(), []).append(value.strip())

    responses: list[str] = []
    others: list[str] = []

    # RFC 3834. Absent means a person; `no` says so explicitly. Anything
    # else -- malformed included -- is not a person, and one such value among
    # repeated headers is enough.
    for value in values.get("auto-submitted", []):
        token = value.split(";", 1)[0].strip().lower()
        if token == "no":
            continue
        if token == "auto-replied":
            responses.append("auto-submitted:auto-replied")
        else:
            others.append(f"auto-submitted:{token[:_TOKEN] or 'empty'}")

    for value in values.get("precedence", []):
        token = value.strip().lower()
        if token == "auto_reply":
            responses.append("precedence:auto_reply")
        elif token in _BULK:
            others.append(f"precedence:{token}")

    if "list-id" in values or "list-unsubscribe" in values:
        others.append("list")

    for value in values.get("x-auto-response-suppress", []):
        tokens = {t.strip().lower() for t in value.split(",")}
        if tokens & _SUPPRESS:
            others.append("x-auto-response-suppress")

    # A null reverse path is how delivery reports are sent (RFC 5321).
    if any(value.replace(" ", "") == "<>" for value in values.get("return-path", [])):
        responses.append("null-return-path")

    local = (sender_email or "").rsplit("@", 1)[0].strip().lower()
    if "@" in (sender_email or "") and local in _REPORTERS:
        responses.append("delivery-report-sender")

    if responses:
        return RuleFinding(
            code=RuleCode.AUTOMATED_RESPONSE,
            severity=Severity.LOW,
            description=(
                "Réponse automatique ou rapport de remise : rien à instruire, "
                "aucune réponse automatique."
            ),
            evidence={"signals": responses + others},
        )
    if others:
        return RuleFinding(
            code=RuleCode.AUTOMATED_MESSAGE,
            severity=Severity.LOW,
            description="Message automatique ou de masse : jamais de réponse automatique.",
            evidence={"signals": others},
        )
    return None

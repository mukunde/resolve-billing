"""Who is calling, established by authentication, never by the request body.

Two kinds of caller reach this API, and they may do different things:

- **the orchestrator** (n8n today, a durable workflow in ADR-010) starts an
  investigation and asks for an approved action to be executed. It presents a
  shared service key: ``Authorization: Bearer <key>``.
- **an approver** -- a person -- reads the proposal and decides. In this
  demonstrator a person signs in with HTTP Basic against a configured list. In
  production that is the company's identity provider (OIDC), behind the same
  :class:`ActorContext`, so no route changes when it is swapped.

Every route that acts takes an :class:`ActorContext` from here. The identity
recorded against a decision is the authenticated one: a name typed into a form
or a JSON field is not identity, and is no longer read. See ADR-009.

Closed by default: a route whose kind of caller is not configured answers 503
rather than letting anyone through.

Configuration, from the environment:

- ``RESOLVE_SERVICE_KEY``: the orchestrator's key.
- ``RESOLVE_APPROVERS``: ``name:secret`` pairs, comma-separated.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request

Kind = Literal["service", "approver"]

#: The identity recorded when the orchestrator acts.
ORCHESTRATOR = "orchestrator"


@dataclass(frozen=True)
class ActorContext:
    """The authenticated caller: who, and what kind of caller."""

    identity: str
    kind: Kind


def _service_key() -> str | None:
    return os.getenv("RESOLVE_SERVICE_KEY") or None


def _approvers() -> dict[str, str]:
    approvers: dict[str, str] = {}
    for entry in os.getenv("RESOLVE_APPROVERS", "").split(","):
        name, separator, secret = entry.strip().partition(":")
        if name and separator and secret:
            approvers[name] = secret
    return approvers


def _same(given: str, expected: str) -> bool:
    return secrets.compare_digest(given.encode(), expected.encode())


def authenticate(request: Request) -> ActorContext | None:
    """The caller behind the Authorization header, or None."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    scheme = scheme.lower()

    if scheme == "bearer":
        key = _service_key()
        if key and value and _same(value, key):
            return ActorContext(ORCHESTRATOR, "service")
        return None

    if scheme == "basic":
        try:
            name, _, secret = base64.b64decode(value, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        expected = _approvers().get(name)
        # Compared even for an unknown name, so the answer takes as long
        # whether or not the name exists.
        matches = _same(secret, expected if expected is not None else secret + "\x00")
        if expected is not None and matches:
            return ActorContext(name, "approver")
        return None

    return None


def _configured(kind: Kind) -> bool:
    return bool(_service_key()) if kind == "service" else bool(_approvers())


def requires(*kinds: Kind):
    """A dependency that admits only these kinds of caller."""

    def dependency(request: Request) -> ActorContext:
        if not any(_configured(kind) for kind in kinds):
            raise HTTPException(
                503, f"authentication is not configured for: {', '.join(kinds)}"
            )
        actor = authenticate(request)
        if actor is None:
            # A browser offers a sign-in prompt only where a person may sign in.
            challenge = (
                'Basic realm="ResolveBilling", charset="UTF-8"'
                if "approver" in kinds
                else 'Bearer realm="ResolveBilling"'
            )
            raise HTTPException(
                401, "authentication required", headers={"WWW-Authenticate": challenge}
            )
        if actor.kind not in kinds:
            raise HTTPException(403, f"a {actor.kind} may not do this")
        return actor

    return dependency


Service = Annotated[ActorContext, Depends(requires("service"))]
Approver = Annotated[ActorContext, Depends(requires("approver"))]
Anyone = Annotated[ActorContext, Depends(requires("service", "approver"))]

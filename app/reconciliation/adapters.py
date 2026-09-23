"""Two ways to get the billed figures, one parser between them.

`FileUsageReport` reads a cost-report response someone saved. That is the one
that runs today, and it is not a fallback: finance hands over an export far
more often than it hands over a credential, and a reconciliation that only
works with live API access is a reconciliation most organisations cannot run.

`AnthropicAdminUsageReport` calls the endpoint. **It has never been run
against the live API**, and that is stated here rather than discovered later --
the same warning this repository carried about its decision engine until the
first real call, which then found three defects in an afternoon. Treat it as
unverified.

The reason it is unverified is worth recording, because it is a design
consequence rather than an oversight: the Usage and Cost endpoints reject
**workspace-scoped API keys**. This project deliberately uses one, for the
spend cap and for attributability. Running this adapter needs an Admin API
key, an `org:admin` OAuth token, or a key not bound to a workspace -- a
credential escalation that a demonstration does not justify.

Raw HTTP on purpose: these two endpoints are not in any Anthropic SDK.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from app.reconciliation.base import BilledLine, parse_cost_report

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 30.0

#: Sent so the provider can see what is calling. Asked for in their docs, and
#: cheap courtesy towards whoever reads those logs.
USER_AGENT = "agentic-billing-exception-handler/0.1 (+reconciliation)"


class FileUsageReport:
    """A cost-report response read from disk."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def billed(self, start: date, end: date) -> list[BilledLine]:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        lines = parse_cost_report(payload)
        # Filtered here rather than trusted: a saved export covers whatever
        # range it was pulled for, which is rarely the one being reconciled.
        return [line for line in lines if start <= line.day < end]


class AnthropicAdminUsageReport:
    """The Usage & Cost Admin API. Unverified against the live endpoint.

    Pagination is followed to the end: `has_more` with an ignored `next_page`
    silently truncates a bill, and a reconciliation run on a truncated bill
    reports a deficit that does not exist.
    """

    name = "anthropic-admin"

    def __init__(self, api_key: str | None = None, max_pages: int = 50) -> None:
        self._api_key = api_key or os.getenv("ANTHROPIC_ADMIN_KEY")
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_ADMIN_KEY is not set. The Usage and Cost endpoints "
                "reject workspace-scoped keys; they need an Admin API key, an "
                "org:admin OAuth token, or a key not bound to a workspace."
            )
        self._max_pages = max_pages

    def billed(self, start: date, end: date) -> list[BilledLine]:
        lines: list[BilledLine] = []
        page: str | None = None

        for _ in range(self._max_pages):
            payload = self._get(start, end, page)
            lines.extend(parse_cost_report(payload))
            if not payload.get("has_more"):
                return lines
            page = payload.get("next_page")
            if not page:
                return lines

        raise RuntimeError(
            f"cost report still paginating after {self._max_pages} pages; "
            "narrow the date range rather than raising this blindly"
        )

    def _get(self, start: date, end: date, page: str | None) -> dict:
        params = [
            ("starting_at", f"{start.isoformat()}T00:00:00Z"),
            ("ending_at", f"{end.isoformat()}T00:00:00Z"),
            ("bucket_width", "1d"),
            ("group_by[]", "description"),
            ("group_by[]", "workspace_id"),
        ]
        if page:
            params.append(("page", page))

        request = urllib.request.Request(
            f"{COST_REPORT_URL}?{urllib.parse.urlencode(params)}",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "user-agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())

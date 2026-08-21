"""Business logic for the example app.

Routes stay thin: they parse input and pick a template. Everything else —
talking to appkit, shaping data, composing the email — lives here so it can be
unit-tested without a web server. This module only ever calls into ``appkit``;
it never imports ``httpx`` or ``psycopg`` (see AGENTS.md).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from appkit import mail, sharepoint

# The SharePoint list this app reads from.
REQUESTS_LIST = "Requests"

# Column contract shared by the table macro and the email summary.
COLUMNS: list[dict[str, str]] = [
    {"key": "Title", "label": "Request"},
    {"key": "Requester", "label": "Requester"},
    {"key": "Department", "label": "Department"},
    {"key": "Status", "label": "Status"},
    {"key": "Submitted", "label": "Submitted"},
]


@dataclass(frozen=True)
class Summary:
    """A computed summary of the current requests."""

    total: int
    by_status: dict[str, int]
    text: str


def list_requests() -> list[dict]:
    """Return the rows of the Requests SharePoint list."""
    return sharepoint.list_rows(REQUESTS_LIST)


def summarize(rows: list[dict]) -> Summary:
    """Build a status summary and a plain-text body from the given rows."""
    by_status = dict(Counter(row.get("Status", "Unknown") for row in rows))
    lines = [f"Requests summary — {len(rows)} total", ""]
    for status, count in sorted(by_status.items()):
        lines.append(f"  {status}: {count}")
    lines.append("")
    for row in rows:
        lines.append(
            f"- {row.get('Title', '(untitled)')}"
            f" [{row.get('Status', 'Unknown')}]"
            f" — {row.get('Requester', 'unknown')}"
        )
    return Summary(total=len(rows), by_status=by_status, text="\n".join(lines))


def send_summary(recipient: str) -> Summary:
    """Compute a summary of the current requests and email it to ``recipient``."""
    recipient = (recipient or "").strip()
    if "@" not in recipient:
        raise ValueError("Enter a valid email address.")

    summary = summarize(list_requests())
    mail.send_mail(
        to=recipient,
        subject=f"Requests summary ({summary.total} open items)",
        body=summary.text,
    )
    return summary

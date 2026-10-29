"""The playmaker event stream: reading/tailing events and failure classification.

This module owns the read side of the `events` table (written by
`state.add_event` / `state.update_session`) and the pure classifier that maps a
dispatch failure onto a rerouteable reason.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playmaker import state

LOG_TAIL_BYTES = 4096

# Ordered: the first matching reason wins, so a "429 unauthorized" is a quota
# refusal, not an auth one. Numeric codes are anchored so a digit inside a
# larger number (a cost, an id) cannot fake a reason.
_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "quota",
        (
            r"usage limit",
            r"\bquota\b",
            r"rate[- ]limit",
            r"\b429\b",
            r"too many requests",
            r"limit reached",
            # An API key whose Console balance is empty: HTTP 400
            # invalid_request_error, the same "lane is out" as a 429 for --fallback.
            r"credit balance is too low",
            r"insufficient credit",
        ),
    ),
    (
        "auth",
        (
            r"\b401\b",
            r"\b403\b",
            r"unauthorized",
            r"token expired",
            r"\blogin\b",
            r"oauth",
        ),
    ),
    (
        "model",
        # "model 'x' not found" names a bad --model, not a missing binary, so
        # this scoped pattern must outrank the generic "not found" below.
        (r"model\s+\S+\s+not found",),
    ),
    (
        "binary",
        (
            r"not found",
            r"no such file",
            r"\benoent\b",
        ),
    ),
    (
        "model",
        (
            r"unknown model",
            r"invalid model",
            r"no such model",
            r"model not supported",
        ),
    ),
)

_COMPILED: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (reason, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for reason, patterns in _FAILURE_PATTERNS
)


def classify_failure(*texts: str) -> str:
    """Classify a dispatch failure from the exception text and the log tail.

    Pure: takes the texts it should scan (any of which may be empty), returns
    one of "quota", "auth", "binary", "model", or "unknown".
    """
    blob = "\n".join(text for text in texts if text)
    for reason, patterns in _COMPILED:
        if any(pattern.search(blob) for pattern in patterns):
            return reason
    return "unknown"


def read_log_tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> str:
    """The last `max_bytes` bytes of a log file, or "" when it is unreadable."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def list_events(
    *,
    batch_id: str | None = None,
    session_id: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    after_id: int | None = None,
    max_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Events in id order; filters compose. `limit` keeps the last N matches.

    `after_id` / `max_id` bound the id range exclusively / inclusively, so a
    snapshot listing at `max_id=<cursor>` followed by polls `after_id=<cursor>`
    composes without gaps, overlaps, or out-of-order emissions.
    """
    sql = "SELECT * FROM events"
    where: list[str] = []
    params: list[Any] = []
    if batch_id:
        where.append("batch_id = ?")
        params.append(batch_id)
    if session_id:
        where.append("session_id LIKE ?")
        params.append(f"{session_id}%")
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if after_id is not None:
        where.append("id > ?")
        params.append(after_id)
    if max_id is not None:
        where.append("id <= ?")
        params.append(max_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC"
    with state.connect() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    if since is not None:
        rows = [r for r in rows if (ts := _parse_ts(r["ts"])) is None or ts >= since]
    if limit is not None:
        rows = rows[-limit:] if limit > 0 else []
    return rows


def max_event_id() -> int:
    """The highest event id, or 0 on an empty stream — the follow cursor."""
    with state.connect() as c:
        row = c.execute("SELECT MAX(id) AS m FROM events").fetchone()
    return int(row["m"] or 0)


def event_to_line(row: dict[str, Any]) -> str:
    """One event as a JSON line, detail decoded back to an object."""
    detail = json.loads(row["detail"]) if row["detail"] else None
    return json.dumps(
        {
            "id": row["id"],
            "ts": row["ts"],
            "session_id": row["session_id"],
            "agent": row["agent"],
            "model": row["model"],
            "batch_id": row["batch_id"],
            "kind": row["kind"],
            "status": row["status"],
            "prev_status": row["prev_status"],
            "detail": detail,
        },
        ensure_ascii=False,
    )

"""Insert team-spawned sessions into Zed's sidebar_threads.

Why this exists: Zed's "Import External Agent Threads" filters out
non-interactive runs (`codex exec` is dropped because originator is
`codex_exec` not `codex-tui`/`codex_cli_rs`; `gemini -p` is dropped
because it writes a single-object `.json`, not the interactive `.jsonl`).
For our orchestration we always run non-interactive, so we INSERT
ourselves; Zed reads sidebar_threads at startup and surfaces them in
Thread History.

Visible after the user restarts Zed (or opens a new workspace).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ZED_DB = Path("~/Library/Application Support/Zed/db/0-stable/db.sqlite").expanduser()

# Custom namespace so thread_id is deterministic per (agent, agent_session_id).
# That way re-running register-zed updates instead of creating duplicates.
_NAMESPACE = uuid.UUID("0e6e7d4a-3a3c-4f6a-9c4e-1f7e2c1ab842")

# How `agent` (our internal name) maps to Zed's agent_id column.
_AGENT_ID = {
    "claude": "claude-acp",
    "codex": "codex-acp",
    "gemini": "gemini",
}


def is_available() -> bool:
    return ZED_DB.exists()


def thread_id_for(agent: str, agent_session_id: str) -> bytes:
    """Deterministic 16-byte UUID v5 derived from (agent, agent_session_id)."""
    agent_id = _AGENT_ID.get(agent, agent)
    return uuid.uuid5(_NAMESPACE, f"{agent_id}:{agent_session_id}").bytes


def register(
    *,
    agent: str,
    agent_session_id: str,
    prompt: str,
    cwd: str,
    started_at_iso: str | None = None,
) -> bytes | None:
    """Upsert a row into Zed's sidebar_threads. Returns the thread_id bytes
    on insert/update; returns None if Zed already has a row for this
    (agent_id, session_id) — typically because Zed's native Import already
    picked the session up (Claude is the common case).

    Idempotent: re-running with the same (agent, agent_session_id) only
    refreshes title/timestamps for a row that was originally inserted by
    THIS function.
    """
    if not is_available():
        raise RuntimeError(f"Zed DB not found at {ZED_DB}")

    agent_id = _AGENT_ID.get(agent)
    if agent_id is None:
        raise ValueError(f"unknown agent {agent!r}; supported: {list(_AGENT_ID)}")

    title = _make_title(prompt)
    thread_id = thread_id_for(agent, agent_session_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    created_iso = started_at_iso or now_iso

    conn = sqlite3.connect(str(ZED_DB), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        # Dedup: if Zed already has any row for this session_id+agent_id (e.g.
        # auto-imported), don't add a parallel row. Restrict the check to a
        # different thread_id so our own re-registers still upsert.
        existing = conn.execute(
            "SELECT thread_id FROM sidebar_threads "
            "WHERE session_id = ? AND agent_id = ? AND thread_id != ? LIMIT 1",
            (agent_session_id, agent_id, thread_id),
        ).fetchone()
        if existing is not None:
            return None
        conn.execute(
            """
            INSERT INTO sidebar_threads (
                thread_id, session_id, agent_id, title,
                updated_at, created_at, interacted_at,
                folder_paths, folder_paths_order,
                main_worktree_paths, main_worktree_paths_order,
                archived
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(thread_id) DO UPDATE SET
                title = excluded.title,
                updated_at = excluded.updated_at,
                interacted_at = excluded.interacted_at,
                folder_paths = excluded.folder_paths,
                main_worktree_paths = excluded.main_worktree_paths
            """,
            (
                thread_id,
                agent_session_id,
                agent_id,
                title,
                now_iso,
                created_iso,
                now_iso,
                cwd,
                "0",
                cwd,
                "0",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return thread_id


def _make_title(prompt: str, *, limit: int = 80) -> str:
    cleaned = (prompt or "").replace("\n", " ").strip()
    if not cleaned:
        return "(untitled)"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."

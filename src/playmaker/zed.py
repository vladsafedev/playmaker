"""Insert playmaker-spawned sessions into Zed's sidebar_threads.

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
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ZED_DB = Path("~/Library/Application Support/Zed/db/0-stable/db.sqlite").expanduser()

# Custom namespace so thread_id is deterministic per (agent, agent_session_id).
# That way re-running register-zed updates instead of creating duplicates.
_NAMESPACE = uuid.UUID("0e6e7d4a-3a3c-4f6a-9c4e-1f7e2c1ab842")

# How `agent` (our internal name) maps to Zed's agent_id column.
#
# Phase 2 (strategic): we keep the NATIVE agent_id values so Zed UI shows
# the proper branded icon (Claude ✦, Codex ⊕, Gemini ✷) for each
# dispatched thread. Routing of sidebar clicks to playmaker (vs native
# binary) is done via `agent_servers` settings — the user replaces the
# native registry registration with a custom one pointing at
# `playmaker acp --agent <name>`. See docs/acp-phase2.md §9 for the
# settings.json snippet.
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

    # 🟢 marker stays until `finalize()` is called on terminal status.
    title = f"{RUNNING_PREFIX}{_make_title(prompt, limit=80 - len(RUNNING_PREFIX))}"
    thread_id = thread_id_for(agent, agent_session_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    created_iso = started_at_iso or now_iso

    conn = sqlite3.connect(str(ZED_DB), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        # Archive any native-Import duplicate for the same session_id. Zed's
        # "External Agent Threads" Import automatically indexes claude jsonl
        # files in ~/.claude/projects/, creating a parallel row with
        # agent_id="claude-acp". When playmaker is the routing target
        # (corr-Phase2), that parallel row confuses the user — clicking it
        # opens native claude-acp instead of our proxy. Solution: hide the
        # native row by archiving it. We don't DELETE because the user may
        # un-archive it manually if they want to inspect via the native path.
        conn.execute(
            "UPDATE sidebar_threads SET archived = 1 "
            "WHERE session_id = ? AND thread_id != ? AND archived = 0",
            (agent_session_id, thread_id),
        )
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


# Prefixed onto a thread's sidebar title while the dispatched sub-agent is
# running, stripped on terminal status (done/failed/killed). Lets the user
# see which sidebar rows are live without opening them — Zed UI doesn't
# render any spinner/loader on closed sidebar rows otherwise.
RUNNING_PREFIX = "🟢 "


def finalize(*, agent: str, agent_session_id: str) -> bool:
    """Strip the running-marker prefix from this thread's sidebar title.

    Called by `_run_dispatch` once the sub-agent transitions to a terminal
    state (done / failed / killed). Returns True if a row was updated.
    Idempotent: no-op if the prefix is already absent.

    Prints a diagnostic to stderr (visible in ~/.playmaker/logs/<sid>.log
    for detached dispatches) — this is intentional, since silent failure
    has bitten us once already (the marker stayed visible until a manual
    UPDATE was run).
    """
    if not is_available():
        sys.stderr.write(f"[zed.finalize] Zed DB unavailable at {ZED_DB}\n")
        return False
    agent_id = _AGENT_ID.get(agent)
    if agent_id is None:
        sys.stderr.write(f"[zed.finalize] unknown agent={agent!r}\n")
        return False
    thread_id = thread_id_for(agent, agent_session_id)
    conn = sqlite3.connect(str(ZED_DB), timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        cur = conn.execute(
            "UPDATE sidebar_threads SET title = substr(title, ?) "
            "WHERE thread_id = ? AND substr(title, 1, ?) = ?",
            (len(RUNNING_PREFIX) + 1, thread_id, len(RUNNING_PREFIX), RUNNING_PREFIX),
        )
        conn.commit()
        rowcount = cur.rowcount
        sys.stderr.write(
            f"[zed.finalize] agent={agent} sid={agent_session_id[:8]} "
            f"thread_id={thread_id.hex()[:8]} rows_updated={rowcount}\n"
        )
        return rowcount > 0
    except Exception as exc:
        sys.stderr.write(
            f"[zed.finalize] agent={agent} sid={agent_session_id[:8]} "
            f"FAILED: {type(exc).__name__}: {exc}\n"
        )
        raise
    finally:
        conn.close()

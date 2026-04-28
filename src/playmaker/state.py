"""SQLite-backed run state for `playmaker`."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLAYMAKER_HOME = Path("~/.playmaker").expanduser()
DB_PATH = PLAYMAKER_HOME / "state.db"
LOGS_DIR = PLAYMAKER_HOME / "logs"
OUTPUTS_DIR = PLAYMAKER_HOME / "outputs"
AGENTS_DIR = PLAYMAKER_HOME / "agents"
CONFIG_PATH = PLAYMAKER_HOME / "config.toml"
QUOTAS_PATH = PLAYMAKER_HOME / "quotas.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    agent_session_id TEXT,
    prompt TEXT NOT NULL,
    cwd TEXT NOT NULL,
    files TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    cost_usd REAL,
    duration_seconds REAL,
    output_path TEXT,
    session_file_path TEXT,
    parent_id TEXT,
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_started ON sessions(started_at DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def ensure_dirs() -> None:
    for d in (PLAYMAKER_HOME, LOGS_DIR, OUTPUTS_DIR, AGENTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    ensure_dirs()
    with connect() as c:
        c.executescript(SCHEMA)
        c.commit()


def insert_session(
    *,
    agent: str,
    prompt: str,
    cwd: str,
    files: list[str] | None = None,
    parent_id: str | None = None,
) -> str:
    """Insert a new pending session, return its id."""
    sid = new_session_id()
    with connect() as c:
        c.execute(
            """
            INSERT INTO sessions (
                id, agent, prompt, cwd, files,
                status, started_at, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                agent,
                prompt,
                cwd,
                json.dumps(files) if files else None,
                "pending",
                now_iso(),
                parent_id,
            ),
        )
        c.commit()
    return sid


def update_session(session_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [session_id]
    with connect() as c:
        c.execute(f"UPDATE sessions SET {cols} WHERE id = ?", values)
        c.commit()


def get_session(session_id: str) -> dict[str, Any] | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE id = ? OR id LIKE ?",
            (session_id, f"{session_id}%"),
        ).fetchone()
    return dict(row) if row else None


def get_session_by_agent_session_id(agent_session_id: str) -> dict[str, Any] | None:
    """Lookup by the agent's NATIVE session id (claude UUID, codex thread_id,
    gemini session_id) — what gets written into Zed's `sidebar_threads.session_id`
    and arrives in `session/load` params.
    """
    with connect() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE agent_session_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (agent_session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions(
    *,
    status: str | None = None,
    agent: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sessions"
    where = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]

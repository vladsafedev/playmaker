"""SQLite-backed run state for `playmaker`."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
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
    pid INTEGER,
    model TEXT,
    batch_id TEXT,
    batch_notified INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_started ON sessions(started_at DESC);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        # Migration: pre-existing databases may lack newer columns.
        existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(sessions)")}
        if "model" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
        if "batch_id" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN batch_id TEXT")
        if "batch_notified" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN batch_notified INTEGER")
            # Only on the upgrade run: sessions that finished before this column
            # existed are history — their summary already fired, or never will.
            # Left unclaimed they would land inside the next summary for the
            # same label. Sessions still in flight keep theirs.
            c.execute(
                "UPDATE sessions SET batch_notified = 1 "
                "WHERE status IN ('done', 'failed', 'killed')"
            )
        # Created after migration so it works on pre-existing tables too.
        c.execute("CREATE INDEX IF NOT EXISTS idx_batch ON sessions(batch_id)")
        c.commit()


def insert_session(
    *,
    agent: str,
    prompt: str,
    cwd: str,
    files: list[str] | None = None,
    parent_id: str | None = None,
    model: str | None = None,
    batch_id: str | None = None,
) -> str:
    """Insert a new pending session, return its id."""
    sid = new_session_id()
    with connect() as c:
        c.execute(
            """
            INSERT INTO sessions (
                id, agent, prompt, cwd, files,
                status, started_at, parent_id, model, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                model,
                batch_id,
            ),
        )
        c.commit()
    return sid


def list_batch(batch_id: str) -> list[dict[str, Any]]:
    """The not-yet-summarised sessions of a dispatch batch, oldest first.

    A batch label is a name the user reuses, not a one-off id, so it is the
    unreported sessions — not every session ever tagged — that make up "the
    batch" a summary is about.
    """
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM sessions "
            "WHERE batch_id = ? AND batch_notified IS NULL "
            "ORDER BY started_at ASC",
            (batch_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def claim_batch(session_ids: list[str]) -> bool:
    """Mark these sessions summarised; True if this caller is the one that did.

    Every detached dispatch finalises its batch as it lands, so two of them can
    see the same drained fan-out. SQLite serialises the write, so the loser
    finds part of its set already claimed; it rolls back and stays quiet rather
    than reporting sessions somebody else has already reported. Sessions
    dispatched into the label after the read are left unclaimed, and become the
    next fan-out.
    """
    if not session_ids:
        return False
    placeholders = ", ".join("?" for _ in session_ids)
    with connect() as c:
        cur = c.execute(
            f"UPDATE sessions SET batch_notified = 1 "
            f"WHERE id IN ({placeholders}) AND batch_notified IS NULL",
            session_ids,
        )
        if cur.rowcount != len(session_ids):
            c.rollback()
            return False
        c.commit()
        return True


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

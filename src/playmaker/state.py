"""SQLite-backed run state for `playmaker`."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
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

TERMINAL_STATUSES = frozenset({"done", "failed", "killed", "no_changes"})

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
    batch_notified INTEGER,
    files_changed INTEGER,
    pre_tree_hash TEXT,
    post_tree_hash TEXT,
    expect_changes INTEGER
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
            placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
            c.execute(
                f"UPDATE sessions SET batch_notified = 1 WHERE status IN ({placeholders})",
                tuple(TERMINAL_STATUSES),
            )
        if "files_changed" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN files_changed INTEGER")
        if "pre_tree_hash" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN pre_tree_hash TEXT")
        if "post_tree_hash" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN post_tree_hash TEXT")
        if "expect_changes" not in existing_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN expect_changes INTEGER")
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
    expect_changes: bool | None = None,
) -> str:
    """Insert a new pending session, return its id."""
    sid = new_session_id()
    with connect() as c:
        c.execute(
            """
            INSERT INTO sessions (
                id, agent, prompt, cwd, files,
                status, started_at, parent_id, model, batch_id, expect_changes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(expect_changes) if expect_changes is not None else None,
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


_WRITE_TASK_RE = re.compile(
    r"\b(?:implement|add|create|write|edit|fix|refactor|build|feature|deliverable|acceptance)\b",
    re.IGNORECASE,
)
_READ_ONLY_TASK_RE = re.compile(
    r"\b(?:recon only|do not edit|do not modify|read-only|read only|answer only|reply with)\b",
    re.IGNORECASE,
)


def expects_changes(prompt: str, explicit: bool | None) -> bool:
    """Return whether a session should be expected to modify its working tree."""
    if explicit is not None:
        return explicit
    # TODO: profiles are currently body-only Markdown, so they cannot supply a default yet.
    return bool(_WRITE_TASK_RE.search(prompt)) and not bool(_READ_ONLY_TASK_RE.search(prompt))


_FALLBACK_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
_FALLBACK_MAX_ENTRIES = 20_000


@dataclass
class WorkingTreeSnapshot:
    """One bounded pre/post view of an agent working directory."""

    cwd: Path
    kind: str
    tree_hash: str | None
    git_status: dict[str, tuple[str, int | None, int | None]] | None = None
    head: str | None = None
    paths: dict[str, int] | None = None
    marker_path: Path | None = None
    marker_mtime_ns: int | None = None

    def cleanup(self) -> None:
        """Remove the external mtime marker, if this is a pre-run fallback view."""
        if self.marker_path is None:
            return
        try:
            self.marker_path.unlink()
        except OSError:
            pass


def take_working_tree_snapshot(cwd: Path, *, with_marker: bool = True) -> WorkingTreeSnapshot:
    """Capture git status or a bounded mtime fallback without raising.

    The marker is deliberately outside ``cwd``: a write detector must not be
    the only file that makes an otherwise no-op worker look successful.
    """
    cwd = cwd.expanduser()
    if _is_git_work_tree(cwd):
        snapshot = _take_git_snapshot(cwd)
        if snapshot is not None:
            return snapshot
    return _take_mtime_snapshot(cwd, with_marker=with_marker)


def count_working_tree_changes(
    before: WorkingTreeSnapshot, after: WorkingTreeSnapshot
) -> int | None:
    """Count paths changed between snapshots; ``None`` means the view is unknown."""
    if before.kind != after.kind or before.cwd != after.cwd:
        return None
    if before.kind == "git":
        if before.git_status is None or after.git_status is None:
            return None
        changed = {
            path
            for path in set(before.git_status) | set(after.git_status)
            if before.git_status.get(path) != after.git_status.get(path)
        }
        if before.head != after.head:
            committed_paths = _paths_changed_since_head(before.cwd, before.head, after.head)
            if committed_paths is None:
                return None
            changed.update(committed_paths)
        return len(changed)
    if before.kind == "mtime":
        if before.paths is None or after.paths is None or before.marker_mtime_ns is None:
            return None
        changed = set(before.paths) ^ set(after.paths)
        changed.update(
            path for path, mtime_ns in after.paths.items() if mtime_ns > before.marker_mtime_ns
        )
        return len(changed)
    return None


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _is_git_work_tree(cwd: Path) -> bool:
    proc = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    return bool(proc and proc.returncode == 0 and proc.stdout.strip() == "true")


def _take_git_snapshot(cwd: Path) -> WorkingTreeSnapshot | None:
    status_z = _run_git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status_z is None or status_z.returncode != 0:
        return None
    head_proc = _run_git(cwd, "rev-parse", "HEAD")
    head = head_proc.stdout.strip() if head_proc and head_proc.returncode == 0 else None
    git_status = {
        path: (status, *_git_path_identity(cwd, path))
        for path, status in _parse_porcelain_z(status_z.stdout).items()
    }
    return WorkingTreeSnapshot(
        cwd=cwd,
        kind="git",
        tree_hash=_git_tree_hash(git_status, head),
        git_status=git_status,
        head=head,
    )


def _parse_porcelain_z(output: str) -> dict[str, str]:
    """Map each changed path to its porcelain status, including rename endpoints."""
    entries = output.split("\0")
    paths: dict[str, str] = {}
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry or len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths[path] = status
        if "R" in status or "C" in status:
            if index < len(entries) and entries[index]:
                paths[entries[index]] = status
            index += 1
    return paths


def _git_path_identity(cwd: Path, path: str) -> tuple[int | None, int | None]:
    try:
        stat = (cwd / path).lstat()
    except OSError:
        return None, None
    return stat.st_mtime_ns, stat.st_size


def _git_tree_hash(
    git_status: dict[str, tuple[str, int | None, int | None]], head: str | None
) -> str:
    digest = hashlib.sha256()
    for path, (status, mtime_ns, size) in sorted(git_status.items()):
        digest.update(path.encode("utf-8", "surrogateescape"))
        digest.update(f"\0{status}\0{mtime_ns}\0{size}\n".encode())
    digest.update(f"HEAD\0{head or ''}".encode())
    return digest.hexdigest()


def _paths_changed_since_head(
    cwd: Path, before_head: str | None, after_head: str | None
) -> set[str] | None:
    if before_head and after_head:
        proc = _run_git(cwd, "diff", "--name-only", before_head, after_head)
    elif before_head:
        proc = _run_git(cwd, "diff", "--name-only", before_head)
    elif after_head:
        proc = _run_git(
            cwd,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "--root",
            after_head,
        )
    else:
        return set()
    if proc is None or proc.returncode != 0:
        return None
    return {path for path in proc.stdout.splitlines() if path}


def _take_mtime_snapshot(cwd: Path, *, with_marker: bool) -> WorkingTreeSnapshot:
    marker_path: Path | None = None
    marker_mtime_ns: int | None = None
    if with_marker:
        try:
            with tempfile.NamedTemporaryFile(prefix="playmaker-tree-", delete=False) as marker:
                marker_path = Path(marker.name)
            marker_mtime_ns = marker_path.stat().st_mtime_ns
        except OSError:
            return WorkingTreeSnapshot(cwd=cwd, kind="unknown", tree_hash=None)

    paths = _walk_file_mtimes(cwd)
    if paths is None:
        if marker_path is not None:
            try:
                marker_path.unlink()
            except OSError:
                pass
        return WorkingTreeSnapshot(cwd=cwd, kind="unknown", tree_hash=None)
    return WorkingTreeSnapshot(
        cwd=cwd,
        kind="mtime",
        tree_hash=_mtime_tree_hash(paths),
        paths=paths,
        marker_path=marker_path,
        marker_mtime_ns=marker_mtime_ns,
    )


def _walk_file_mtimes(cwd: Path) -> dict[str, int] | None:
    try:
        if not cwd.is_dir():
            return None
        paths: dict[str, int] = {}
        entries = 0
        for root, dirs, files in os.walk(cwd, onerror=_raise_walk_error):
            dirs[:] = [name for name in dirs if name not in _FALLBACK_SKIP_DIRS]
            for name in files:
                entries += 1
                if entries > _FALLBACK_MAX_ENTRIES:
                    return None
                path = Path(root) / name
                paths[str(path.relative_to(cwd))] = path.stat().st_mtime_ns
        return paths
    except OSError:
        return None


def _mtime_tree_hash(paths: dict[str, int]) -> str:
    digest = hashlib.sha256()
    for path, mtime_ns in sorted(paths.items()):
        digest.update(f"{path}\0{mtime_ns}\n".encode())
    return digest.hexdigest()


def _raise_walk_error(error: OSError) -> None:
    raise error

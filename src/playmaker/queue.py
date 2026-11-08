"""Per-thread follow-up queue storage and delivery."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from playmaker import state
from playmaker.agents.codex import VALID_EFFORTS as CODEX_VALID_EFFORTS
from playmaker.config import agent_setting


def _renumber(c, thread_id: str) -> None:
    rows = c.execute(
        "SELECT id FROM queue WHERE thread_id = ? AND status = 'queued' ORDER BY position, id",
        (thread_id,),
    ).fetchall()
    for pos, row in enumerate(rows, 1):
        c.execute("UPDATE queue SET position = ? WHERE id = ?", (pos, row["id"]))


def enqueue(
    thread_id: str,
    prompt: str,
    *,
    cwd: str | None = None,
    files: list[str] | None = None,
    gate_cmd: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    expect_changes: bool | None = None,
    first: bool = False,
    position: int | None = None,
) -> dict[str, Any]:
    if position is not None and position < 1:
        raise ValueError("position must be at least 1")
    with state.connect() as c:
        count = c.execute(
            "SELECT count(*) FROM queue WHERE thread_id=? AND status='queued'", (thread_id,)
        ).fetchone()[0]
        pos = 1 if first else (max(1, min(position, count + 1)) if position else count + 1)
        c.execute(
            "UPDATE queue SET position=position+1 WHERE thread_id=? "
            "AND status='queued' AND position>=?",
            (thread_id, pos),
        )
        cur = c.execute(
            """INSERT INTO queue (thread_id, position, prompt, cwd, files, gate_cmd,
            model, effort, expect_changes, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
            (
                thread_id,
                pos,
                prompt,
                cwd,
                json.dumps(files) if files else None,
                gate_cmd,
                model,
                effort,
                int(expect_changes) if expect_changes is not None else None,
                state.now_iso(),
            ),
        )
        _renumber(c, thread_id)
        row = c.execute("SELECT * FROM queue WHERE id=?", (cur.lastrowid,)).fetchone()
        c.commit()
    return dict(row)


def get(item_id: int) -> dict[str, Any] | None:
    with state.connect() as c:
        row = c.execute("SELECT * FROM queue WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items(thread_id: str | None = None, *, all_: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM queue" + (" WHERE thread_id=?" if thread_id else "")
    params: list[Any] = [thread_id] if thread_id else []
    sql += (
        " AND status='queued'"
        if thread_id and not all_
        else (" WHERE status='queued'" if not all_ else "")
    )
    sql += " ORDER BY thread_id, position, id"
    with state.connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _mutable(item_id: int) -> dict[str, Any]:
    row = get(item_id)
    if row is None:
        raise ValueError(f"queue item #{item_id} not found")
    if row["status"] != "queued":
        raise ValueError(f"queue item #{item_id} is {row['status']} and immutable")
    return row


def edit(item_id: int, **fields: Any) -> dict[str, Any]:
    row = _mutable(item_id)
    allowed = {k: v for k, v in fields.items() if v is not None}
    if allowed:
        if "files" in allowed:
            allowed["files"] = json.dumps(allowed["files"]) if allowed["files"] else None
        cols = ", ".join(f"{k}=?" for k in allowed)
        with state.connect() as c:
            c.execute(
                f"UPDATE queue SET {cols}, updated_at=? WHERE id=?",
                [*allowed.values(), state.now_iso(), item_id],
            )
            c.commit()
    return get(item_id) or row


def cancel(item_id: int) -> dict[str, Any]:
    row = _mutable(item_id)
    with state.connect() as c:
        c.execute(
            "UPDATE queue SET status='cancelled', updated_at=? WHERE id=?",
            (state.now_iso(), item_id),
        )
        _renumber(c, row["thread_id"])
        c.commit()
    return get(item_id) or row


def clear(thread_id: str) -> int:
    with state.connect() as c:
        cur = c.execute(
            "UPDATE queue SET status='cancelled', updated_at=? WHERE thread_id=? "
            "AND status='queued'",
            (state.now_iso(), thread_id),
        )
        _renumber(c, thread_id)
        c.commit()
        return cur.rowcount


def move(item_id: int, position: int) -> dict[str, Any]:
    if position < 1:
        raise ValueError("position must be at least 1")
    row = _mutable(item_id)
    with state.connect() as c:
        count = c.execute(
            "SELECT count(*) FROM queue WHERE thread_id=? AND status='queued'", (row["thread_id"],)
        ).fetchone()[0]
        c.execute("UPDATE queue SET position=? WHERE id=?", (max(1, min(position, count)), item_id))
        # Stable desired ordering requires moving aside first.
        rows = c.execute(
            "SELECT id FROM queue WHERE thread_id=? AND status='queued' AND id != ? "
            "ORDER BY position, id",
            (row["thread_id"], item_id),
        ).fetchall()
        ids = [r["id"] for r in rows]
        ids.insert(max(0, min(position - 1, len(ids))), item_id)
        for pos, ident in enumerate(ids, 1):
            c.execute("UPDATE queue SET position=? WHERE id=?", (pos, ident))
        c.commit()
    return get(item_id) or row


def queued_count(thread_id: str) -> int:
    with state.connect() as c:
        return c.execute(
            "SELECT count(*) FROM queue WHERE thread_id=? AND status='queued'", (thread_id,)
        ).fetchone()[0]


def deliver_next(thread_id: str, parent_id: str, spawn: Callable[[str], int]) -> str | None:
    """Atomically claim one item, make its resume child, then spawn it."""
    with state.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        parent = c.execute("SELECT * FROM sessions WHERE id=?", (parent_id,)).fetchone()
        if parent is None or not parent["agent_session_id"]:
            c.rollback()
            return None
        parent = dict(parent)
        live = c.execute(
            """WITH RECURSIVE thread(id) AS (
                SELECT id FROM sessions WHERE id=?
                UNION ALL
                SELECT sessions.id FROM sessions JOIN thread ON sessions.parent_id=thread.id
            ) SELECT 1 FROM sessions WHERE id IN (SELECT id FROM thread)
              AND status IN ('pending', 'running') LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if live is not None:
            c.rollback()
            return None
        item = c.execute(
            "SELECT * FROM queue WHERE thread_id=? AND status='queued' "
            "ORDER BY position, id LIMIT 1",
            (thread_id,),
        ).fetchone()
        if item is None:
            return None
        item = dict(item)
        delivered_effort = item.get("effort")
        if parent["agent"] == "codex" and delivered_effort is None:
            delivered_effort = parent.get("effort")
            if delivered_effort is None:
                configured = agent_setting("codex", "reasoning_effort")
                normalized = str(configured).strip() if configured is not None else None
                if normalized in CODEX_VALID_EFFORTS:
                    delivered_effort = normalized
        sid = state.new_session_id()
        claimed = c.execute(
            "UPDATE queue SET status='delivered', delivered_as=?, updated_at=? "
            "WHERE id=? AND status='queued'",
            (sid, state.now_iso(), item["id"]),
        )
        if claimed.rowcount != 1:
            c.rollback()
            return None
        c.execute(
            """INSERT INTO sessions (id,agent,agent_session_id,prompt,cwd,files,
            status,started_at,parent_id,model,effort,batch_id,expect_changes,worktree_path,
            worktree_branch,repo_root,gate_cmd)
        VALUES (?,?,?,?,?,?, 'pending',?,?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                parent["agent"],
                parent["agent_session_id"],
                item["prompt"],
                item.get("cwd") or parent["cwd"],
                item["files"],
                state.now_iso(),
                parent_id,
                item["model"],
                delivered_effort,
                parent["batch_id"],
                item["expect_changes"],
                parent["worktree_path"],
                parent["worktree_branch"],
                parent["repo_root"],
                item["gate_cmd"],
            ),
        )
        _renumber(c, thread_id)
        c.commit()
    try:
        spawn(sid)
    except Exception:
        # One BEGIN IMMEDIATE transaction: the child flips to failed in the
        # same commit that returns the item to `queued`, so a concurrent
        # `queue drain --force` can never see the child failed while item #1
        # is still `delivered` and hand item #2 to a worker first.
        with state.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
            prev_status = row["status"] if row is not None else None
            if prev_status != "failed":
                state._append_event(
                    c,
                    sid,
                    "status",
                    status="failed",
                    prev_status=prev_status,
                    detail={"reason": "spawn"},
                )
            c.execute(
                "UPDATE sessions SET status='failed', finished_at=?, exit_code=1, "
                "fail_reason='spawn' WHERE id=?",
                (state.now_iso(), sid),
            )
            c.execute(
                "UPDATE queue SET status='queued', delivered_as=NULL, updated_at=? WHERE id=?",
                (state.now_iso(), item["id"]),
            )
            _renumber(c, thread_id)
            c.commit()
        state.add_event(
            parent_id, "queue_blocked", detail={"thread_id": thread_id, "reason": "spawn"}
        )
        return None
    state.add_event(parent_id, "queue_delivered", detail={"item": item["id"], "session_id": sid})
    return sid


def drain_idle_threads(
    spawn: Callable[[str], int], *, thread_id: str | None = None, force: bool = False
) -> list[str]:
    """Deliver one item for each eligible idle thread."""
    rows = list_items(thread_id, all_=False)
    roots = list(dict.fromkeys(r["thread_id"] for r in rows))
    delivered: list[str] = []
    for root in roots:
        sessions = state.thread_sessions(root)
        if not sessions or any(s["status"] in {"pending", "running"} for s in sessions):
            continue
        latest = max(sessions, key=lambda s: s["started_at"])
        if latest["status"] in {"done", "no_changes"} or (force and latest["status"] == "failed"):
            sid = deliver_next(root, latest["id"], spawn)
            if sid:
                delivered.append(sid)
    return delivered

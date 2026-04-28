"""ACP server event loop — Phase 2 (replay-driven, sidebar-aware).

Phase 1 was a transparent single-thread proxy where the user opened threads
through the Plus-menu. Phase 2 pivot: playmaker is a proper agent that owns
session lifecycle, serves `session/load` from playmaker's `state.db` (filled
by `playmaker dispatch`), and on follow-up `session/prompt` reopens the
right child via `handler.resume()`.

See docs/acp-phase2.md for design details and the canonical reference
captures in ~/acp-logs/claude-20260428-224329.{in,out}.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from playmaker import state as state_db
from playmaker.acp.caps import initialize_response
from playmaker.acp.child import (
    ChildHandle,
    ChildSpawnError,
    shutdown_child,
    spawn_child,
    stderr_tail_text,
)
from playmaker.acp.replay import turns_to_updates
from playmaker.acp.session_map import ChildSession, SessionMap
from playmaker.registry import get_handler

logger = logging.getLogger("playmaker.acp.proxy")


# Phase 1 child for Plus-menu threads (session/new path) — Claude only in MVP.
DEFAULT_CHILD_CMD: list[str] = [
    "/Users/shulyugin/.nvm/versions/node/v24.1.0/bin/npm",
    "exec",
    "@agentclientprotocol/claude-agent-acp@0.31.3",
]

# LRU pool / idle-timeout policy.
MAX_CHILDREN = 3
IDLE_TIMEOUT_SECONDS = 300.0  # 5 minutes
IDLE_SWEEP_INTERVAL = 60.0


MsgKind = Literal["request", "response", "notification", "error"]


def classify(msg: dict[str, Any]) -> MsgKind:
    """JSON-RPC discriminated union (corr-4). Pending tables touched only on
    request/response/error; notifications never index pending."""
    if "method" in msg:
        return "request" if "id" in msg else "notification"
    if "result" in msg:
        return "response"
    if "error" in msg:
        return "error"
    raise ValueError(f"unclassifiable jsonrpc message: keys={list(msg.keys())}")


@dataclass
class ProxyState:
    sessions: SessionMap = field(default_factory=SessionMap)
    # Recorded Zed `initialize` request — replayed verbatim when we spawn a
    # child via session/new or via resume-after-load (so the child sees Zed's
    # actual clientCapabilities).
    zed_initialize_request: dict[str, Any] | None = None
    # Pending session-creating requests to a child, keyed by child-side jsonrpc id.
    pending_session_create: dict[int, _PendingSessionCreate] = field(default_factory=dict)
    pending_session_close: dict[int, _PendingSessionClose] = field(default_factory=dict)


@dataclass(frozen=True)
class _PendingSessionCreate:
    zed_id: int
    method: str  # "session/new" | "session/fork" | "session/resume"
    request_session_id: str | None
    child_session: ChildSession


@dataclass(frozen=True)
class _PendingSessionClose:
    zed_id: int
    zed_sid: str


# ---------- Zed-side I/O helpers ---------------------------------------------


async def _write_zed(msg: dict[str, Any]) -> None:
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


async def _read_zed_line(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    raw = await reader.readline()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("malformed JSON from Zed: %s; line=%r", exc, raw[:200])
        return {"_malformed": True}


def _jsonrpc_error(zed_id: int, message: str, code: int = -32000) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": zed_id, "error": {"code": code, "message": message}}


# ---------- LRU pool ---------------------------------------------------------


async def _ensure_pool_capacity(state: ProxyState) -> None:
    """Evict oldest children if we already have MAX_CHILDREN before spawning more."""
    sessions = sorted(state.sessions.all_sessions(), key=lambda s: s.last_activity)
    while len(sessions) >= MAX_CHILDREN:
        oldest = sessions.pop(0)
        zed_pair = state.sessions.by_child(oldest.child_sid)
        zed_sid = zed_pair[0] if zed_pair else None
        logger.info(
            "LRU evict: zed_sid=%s child_sid=%s idle_for=%.1fs",
            zed_sid, oldest.child_sid, time.monotonic() - oldest.last_activity,
        )
        await shutdown_child(oldest.handle)
        if zed_sid:
            state.sessions.unregister(zed_sid)


async def _idle_sweeper(state: ProxyState) -> None:
    """Background task that evicts children idle longer than IDLE_TIMEOUT_SECONDS."""
    while True:
        await asyncio.sleep(IDLE_SWEEP_INTERVAL)
        now = time.monotonic()
        for session in list(state.sessions.all_sessions()):
            if now - session.last_activity > IDLE_TIMEOUT_SECONDS:
                zed_pair = state.sessions.by_child(session.child_sid)
                zed_sid = zed_pair[0] if zed_pair else None
                logger.info(
                    "idle evict: zed_sid=%s child_sid=%s idle_for=%.1fs",
                    zed_sid, session.child_sid, now - session.last_activity,
                )
                await shutdown_child(session.handle)
                if zed_sid:
                    state.sessions.unregister(zed_sid)


# ---------- Zed → us ---------------------------------------------------------


async def _handle_zed_message(state: ProxyState, msg: dict[str, Any]) -> None:
    if msg.get("_malformed"):
        return

    kind = classify(msg)
    method = msg.get("method")

    if kind == "request":
        if method == "initialize":
            await _handle_zed_initialize(state, msg)
            return
        if method == "session/load":
            await _handle_zed_session_load(state, msg)
            return
        if method == "session/new":
            await _handle_zed_session_new(state, msg)
            return
        if method == "session/close":
            await _handle_zed_session_close(state, msg)
            return
        if method == "session/prompt":
            await _handle_zed_session_prompt(state, msg)
            return
        # Other session-targeted requests (e.g. session/set_mode if we ever
        # advertise modes) — generic forward to the existing child.
        await _forward_request_to_child(state, msg)
        return

    if kind == "notification":
        await _forward_notification_to_child(state, msg)
        return

    # Zed answered a child-originated request (fs/*, request_permission).
    await _forward_response_to_child(state, msg)


async def _handle_zed_initialize(state: ProxyState, msg: dict[str, Any]) -> None:
    """Static caps. No child spawn — phase-2 initialize is fast and
    does not require a live agent.
    """
    state.zed_initialize_request = msg
    await _write_zed(initialize_response(msg["id"]))


# ---- session/load: serve from state.db --------------------------------------


async def _handle_zed_session_load(state: ProxyState, msg: dict[str, Any]) -> None:
    """Path B: lookup the session in playmaker's state.db, replay history
    via `session/update` notifications, return rich load response.

    No child spawn here — replay is a pure read of the agent's session-file.
    A follow-up `session/prompt` (if the user types something) will trigger
    `_handle_zed_session_prompt` which spawns a child via `handler.resume()`.
    """
    zed_id = msg["id"]
    params = msg.get("params") or {}
    incoming_sid = params.get("sessionId")
    if not incoming_sid:
        await _write_zed(_jsonrpc_error(zed_id, "session/load missing sessionId"))
        return

    # Try state.db lookup. The sid Zed sends is the AGENT's native sid (claude
    # UUID, codex thread_id, gemini sessionId) — that's what playmaker's
    # zed.register() writes into sidebar_threads.session_id.
    row = state_db.get_session_by_agent_session_id(incoming_sid)
    if row is None:
        await _write_zed(_jsonrpc_error(
            zed_id, f"session/load unknown sessionId: {incoming_sid}"
        ))
        return

    session_file = row.get("session_file_path")
    if not session_file or not Path(session_file).exists():
        await _write_zed(_jsonrpc_error(
            zed_id, f"session/load: session_file missing for {incoming_sid}"
        ))
        return

    # Parse turns through the agent-specific handler (Phase 1 work).
    handler = get_handler(row["agent"])
    try:
        turns = handler.parse_session_file(Path(session_file))
    except Exception as exc:
        logger.exception("parse_session_file failed for %s", session_file)
        await _write_zed(_jsonrpc_error(
            zed_id, f"session/load: failed to parse history: {exc}"
        ))
        return

    # Honest replay — no synthesized completions. If the last turn shows the
    # agent died mid-prompt, replay shows that. Zed's "Proceed" UI in that
    # case is a feature, not a bug — it lets us resume via session/prompt.
    for update in turns_to_updates(turns):
        await _write_zed({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": incoming_sid, "update": update},
        })

    # Reply to load with minimal but Claude-shape-compatible result.
    await _write_zed({
        "jsonrpc": "2.0",
        "id": zed_id,
        "result": {
            "sessionId": incoming_sid,
            "modes": None,
            "models": None,
        },
    })

    # Zed expects this signal at end of load (canonical capture confirms).
    # Empty list = no agent-side slash commands; fine for playmaker.
    await _write_zed({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": incoming_sid,
            "update": {"sessionUpdate": "available_commands_update", "availableCommands": []},
        },
    })


# ---- session/prompt: existing OR resume-after-load --------------------------


async def _handle_zed_session_prompt(state: ProxyState, msg: dict[str, Any]) -> None:
    """Two paths:
      1) sid is in SessionMap (Plus-menu thread or already-resumed thread):
         forward the prompt to the live child.
      2) sid is NOT in SessionMap but exists in state.db (resume-after-load):
         spawn a child via handler.resume(), register mapping, forward prompt.
    """
    zed_id = msg["id"]
    params = msg.get("params") or {}
    sid = params.get("sessionId")
    if not sid:
        await _write_zed(_jsonrpc_error(zed_id, "session/prompt missing sessionId"))
        return

    session = state.sessions.by_zed(sid)
    if session is not None:
        # Path 1: existing live child.
        session.touch()
        await _forward_prompt_to_child(state, session, msg)
        return

    # Path 2: resume-after-load. Look up state.db.
    row = state_db.get_session_by_agent_session_id(sid)
    if row is None:
        await _write_zed(_jsonrpc_error(zed_id, f"session/prompt unknown sessionId: {sid}"))
        return

    # Spawn child for this agent and resume.
    await _ensure_pool_capacity(state)
    try:
        handle = await spawn_child(DEFAULT_CHILD_CMD)
    except ChildSpawnError as exc:
        await _write_zed(_jsonrpc_error(zed_id, f"resume: child spawn failed: {exc}"))
        return

    # Replay Zed's initialize so child sees clientCapabilities.
    if state.zed_initialize_request is not None:
        try:
            await handle.send(state.zed_initialize_request)
            init_resp = await handle.recv()
        except Exception as exc:
            logger.exception("resume: child init failed")
            await _write_zed(_jsonrpc_error(zed_id, f"resume: child init failed: {exc}"))
            await shutdown_child(handle)
            return
        if init_resp is None:
            tail = stderr_tail_text(handle)
            await _write_zed(_jsonrpc_error(zed_id, f"resume: child died at init; stderr: {tail}"))
            await shutdown_child(handle)
            return

    # Send a `session/load` to the child first — this is how claude-acp
    # restores its in-memory state for the resumed thread. (Each ACP child
    # reads its own native session-file based on this.)
    load_req_id = handle.alloc_request_id()
    await handle.send({
        "jsonrpc": "2.0",
        "id": load_req_id,
        "method": "session/load",
        "params": {
            "sessionId": sid,
            "cwd": row["cwd"],
            "mcpServers": params.get("mcpServers") or [],
        },
    })
    # Drain child's load reply (may be preceded by replay updates we ignore —
    # Zed already saw our own replay). We loop until we see the response to
    # load_req_id.
    while True:
        try:
            child_msg = await handle.recv()
        except Exception as exc:
            logger.exception("resume: error reading child during load")
            await _write_zed(_jsonrpc_error(zed_id, f"resume: child error during load: {exc}"))
            await shutdown_child(handle)
            return
        if child_msg is None:
            tail = stderr_tail_text(handle)
            await _write_zed(_jsonrpc_error(zed_id, f"resume: child died during load; stderr: {tail}"))
            await shutdown_child(handle)
            return
        if child_msg.get("id") == load_req_id:
            break
        # Else: it's a session/update from child's replay; we drop it (Zed
        # already has our replay). This is the expected path.

    # Register mapping. Use the same sid both ways — child kept it.
    session = ChildSession(child_sid=sid, handle=handle)
    state.sessions.register(zed_sid=sid, session=session)
    asyncio.create_task(
        _drain_child_stdout(state, handle), name=f"child-stdout-{handle.proc.pid}"
    )

    # Now forward the actual prompt.
    session.touch()
    await _forward_prompt_to_child(state, session, msg)


async def _forward_prompt_to_child(
    state: ProxyState, session: ChildSession, msg: dict[str, Any]
) -> None:
    """Forward an in-flight session/prompt to the child (sid already mapped)."""
    zed_id = msg["id"]
    child_id = session.handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    params = dict(forwarded.get("params") or {})
    params["sessionId"] = session.child_sid
    forwarded["params"] = params

    session.pending.record_zed_request(child_id, zed_id=zed_id, method="session/prompt")
    await session.handle.send(forwarded)


# ---- session/new: classic Plus-menu path (Phase 1 logic preserved) ----------


async def _handle_zed_session_new(state: ProxyState, msg: dict[str, Any]) -> None:
    zed_id = msg["id"]
    await _ensure_pool_capacity(state)

    try:
        handle = await spawn_child(DEFAULT_CHILD_CMD)
    except ChildSpawnError as exc:
        await _write_zed(_jsonrpc_error(zed_id, f"child spawn failed: {exc}"))
        return

    if state.zed_initialize_request is not None:
        await handle.send(state.zed_initialize_request)
        init_resp = await handle.recv()
        if init_resp is None:
            tail = stderr_tail_text(handle)
            await _write_zed(_jsonrpc_error(zed_id, f"child died during init; stderr: {tail}"))
            await shutdown_child(handle)
            return

    child_id = handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    params = dict(forwarded.get("params") or {})
    if "mcpServers" in params:
        params["mcpServers"] = list(params["mcpServers"])
    forwarded["params"] = params

    placeholder = ChildSession(child_sid="<pending>", handle=handle)
    state.pending_session_create[child_id] = _PendingSessionCreate(
        zed_id=zed_id,
        method="session/new",
        request_session_id=None,
        child_session=placeholder,
    )
    await handle.send(forwarded)
    _start_drain_once(state, handle)


# ---- session/close ----------------------------------------------------------


async def _handle_zed_session_close(state: ProxyState, msg: dict[str, Any]) -> None:
    zed_id = msg["id"]
    sid = (msg.get("params") or {}).get("sessionId")
    if not sid:
        await _write_zed(_jsonrpc_error(zed_id, "session/close missing sessionId"))
        return
    session = state.sessions.by_zed(sid)
    if session is None:
        # Not in pool — answer success, nothing to do.
        await _write_zed({"jsonrpc": "2.0", "id": zed_id, "result": {}})
        return

    child_id = session.handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    params = dict(forwarded.get("params") or {})
    params["sessionId"] = session.child_sid
    forwarded["params"] = params

    state.pending_session_close[child_id] = _PendingSessionClose(zed_id=zed_id, zed_sid=sid)
    session.closed = True
    await session.handle.send(forwarded)


# ---- generic forwarders (used for less-common methods) ----------------------


async def _forward_request_to_child(state: ProxyState, msg: dict[str, Any]) -> None:
    zed_id = msg["id"]
    sid = (msg.get("params") or {}).get("sessionId")
    if not sid:
        await _write_zed(_jsonrpc_error(zed_id, f"missing sessionId in {msg.get('method')!r}"))
        return
    session = state.sessions.by_zed(sid)
    if session is None:
        await _write_zed(_jsonrpc_error(zed_id, f"unknown sessionId: {sid}"))
        return
    session.touch()

    child_id = session.handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    params = dict(msg["params"])
    params["sessionId"] = session.child_sid
    forwarded["params"] = params
    session.pending.record_zed_request(child_id, zed_id=zed_id, method=msg.get("method", "?"))
    await session.handle.send(forwarded)


async def _forward_notification_to_child(state: ProxyState, msg: dict[str, Any]) -> None:
    """Notifications (session/cancel) — sid rewrite only, no pending change (corr-1)."""
    params = msg.get("params") or {}
    sid = params.get("sessionId")
    if not sid:
        return
    session = state.sessions.by_zed(sid)
    if session is None:
        # Notification for a session we've evicted from pool: drop silently.
        # (Cancel for an evicted session — there's nothing to cancel.)
        return
    session.touch()
    forwarded = dict(msg)
    new_params = dict(params)
    new_params["sessionId"] = session.child_sid
    forwarded["params"] = new_params
    await session.handle.send(forwarded)


async def _forward_response_to_child(state: ProxyState, msg: dict[str, Any]) -> None:
    zed_id = msg.get("id")
    if not isinstance(zed_id, int):
        return
    for session in state.sessions.all_sessions():
        pending = session.pending.take_child_request(zed_id)
        if pending is None:
            continue
        session.touch()
        forwarded = dict(msg)
        forwarded["id"] = pending.child_id
        await session.handle.send(forwarded)
        return
    logger.warning("response from Zed for unknown id=%s", zed_id)


# ---------- child stdout draining --------------------------------------------


def _start_drain_once(
    state: ProxyState, handle: ChildHandle, *, owner: ChildSession | None = None
) -> None:
    """Kick off the single stdout-drain coroutine. Idempotent.

    Sets `_draining=True` synchronously BEFORE creating the task so that a
    second concurrent caller bails out — without this, two readline()
    coroutines would race on the same pipe.
    """
    if getattr(handle, "_draining", False):
        return
    handle._draining = True  # type: ignore[attr-defined]
    asyncio.create_task(
        _drain_child_stdout(state, handle), name=f"child-stdout-{handle.proc.pid}"
    )


async def _drain_child_stdout(state: ProxyState, handle: ChildHandle) -> None:
    """Read every JSON-RPC frame from child stdout and dispatch."""
    try:
        while True:
            try:
                msg = await handle.recv()
            except json.JSONDecodeError as exc:
                logger.error("malformed JSON from child pid=%s: %s", handle.proc.pid, exc)
                continue
            if msg is None:
                break  # EOF
            await _handle_child_message(state, handle, msg)
    finally:
        await _on_child_death(state, handle)


async def _handle_child_message(
    state: ProxyState, handle: ChildHandle, msg: dict[str, Any]
) -> None:
    kind = classify(msg)
    msg_id = msg.get("id")

    # Responses to session/new (or fork/resume in Phase 3).
    if kind in ("response", "error") and isinstance(msg_id, int):
        create = state.pending_session_create.pop(msg_id, None)
        if create is not None:
            await _finalize_session_create(state, handle, msg, create)
            return
        close = state.pending_session_close.pop(msg_id, None)
        if close is not None:
            await _finalize_session_close(state, msg, close)
            return

    # Generic responses — look up via per-session out_to_child.
    if kind in ("response", "error") and isinstance(msg_id, int):
        for session in state.sessions.all_sessions():
            if session.handle is not handle:
                continue
            pending = session.pending.take_zed_request(msg_id)
            if pending is None:
                continue
            session.touch()
            forwarded = dict(msg)
            forwarded["id"] = pending.zed_id
            await _write_zed(forwarded)
            return
        logger.warning("child response with no matching pending: id=%s", msg_id)
        return

    # Child-originated requests (fs/*, request_permission).
    if kind == "request":
        await _forward_request_to_zed(state, handle, msg)
        return

    # Notifications (session/update) — corr-8 byte-for-byte forward.
    if kind == "notification":
        await _forward_notification_to_zed(state, handle, msg)
        return


async def _finalize_session_create(
    state: ProxyState,
    handle: ChildHandle,
    response: dict[str, Any],
    create: _PendingSessionCreate,
) -> None:
    if "error" in response:
        forwarded = dict(response)
        forwarded["id"] = create.zed_id
        await _write_zed(forwarded)
        return

    result = response.get("result") or {}
    new_child_sid = result.get("sessionId")
    if not new_child_sid:
        await _write_zed(_jsonrpc_error(create.zed_id, "child returned no sessionId"))
        return

    zed_sid = SessionMap.mint_zed_sid()
    if create.child_session.child_sid == "<pending>":
        create.child_session.child_sid = new_child_sid
        state.sessions.register(zed_sid=zed_sid, session=create.child_session)
    else:
        new_session = ChildSession(child_sid=new_child_sid, handle=handle)
        state.sessions.register(zed_sid=zed_sid, session=new_session)

    forwarded = dict(response)
    forwarded["id"] = create.zed_id
    new_result = dict(result)
    new_result["sessionId"] = zed_sid
    forwarded["result"] = new_result
    await _write_zed(forwarded)


async def _finalize_session_close(
    state: ProxyState, response: dict[str, Any], close: _PendingSessionClose
) -> None:
    forwarded = dict(response)
    forwarded["id"] = close.zed_id
    await _write_zed(forwarded)
    state.sessions.unregister(close.zed_sid)


async def _forward_request_to_zed(
    state: ProxyState, handle: ChildHandle, msg: dict[str, Any]
) -> None:
    child_id = msg.get("id")
    if not isinstance(child_id, int):
        return

    params = msg.get("params") or {}
    child_sid = params.get("sessionId")
    target: ChildSession | None = None
    target_zed: str | None = None
    if child_sid is not None:
        found = state.sessions.by_child(child_sid)
        if found is not None:
            target_zed, target = found

    if target is None or target.handle is not handle:
        return

    target.touch()
    target.pending.record_child_request(child_id, child_id=child_id, method=msg.get("method", "?"))
    forwarded = dict(msg)
    new_params = dict(params)
    new_params["sessionId"] = target_zed
    forwarded["params"] = new_params
    await _write_zed(forwarded)


async def _forward_notification_to_zed(
    state: ProxyState, handle: ChildHandle, msg: dict[str, Any]
) -> None:
    params = msg.get("params") or {}
    child_sid = params.get("sessionId")
    if child_sid is None:
        await _write_zed(msg)
        return
    found = state.sessions.by_child(child_sid)
    if found is None:
        return
    zed_sid, session = found
    session.touch()
    forwarded = dict(msg)
    new_params = dict(params)
    new_params["sessionId"] = zed_sid
    forwarded["params"] = new_params
    await _write_zed(forwarded)


# ---------- death and shutdown -----------------------------------------------


async def _on_child_death(state: ProxyState, handle: ChildHandle) -> None:
    affected: list[ChildSession] = [
        s for s in state.sessions.all_sessions() if s.handle is handle
    ]
    for session in affected:
        for pending in session.pending.drain_outstanding_zed_requests():
            await _write_zed(_jsonrpc_error(
                pending.zed_id, f"child died (method={pending.method})"
            ))
        session.pending.drain_outstanding_child_requests()
        for pid_, create in list(state.pending_session_create.items()):
            if create.child_session.handle is handle:
                await _write_zed(_jsonrpc_error(create.zed_id, "child died before session/new completed"))
                state.pending_session_create.pop(pid_, None)
        for pid_, close in list(state.pending_session_close.items()):
            sess = state.sessions.by_zed(close.zed_sid)
            if sess is not None and sess.handle is handle:
                await _write_zed(_jsonrpc_error(close.zed_id, "child died before session/close completed"))
                state.pending_session_close.pop(pid_, None)
        zed_pair = state.sessions.by_child(session.child_sid)
        if zed_pair is not None:
            state.sessions.unregister(zed_pair[0])

    logger.warning("child pid=%s died; %d session(s) affected", handle.proc.pid, len(affected))


# ---------- top-level entry --------------------------------------------------


async def run_proxy(child_cmd: list[str] | None = None) -> int:
    if child_cmd is not None:
        global DEFAULT_CHILD_CMD
        DEFAULT_CHILD_CMD = child_cmd

    state = ProxyState()
    loop = asyncio.get_running_loop()

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    sweeper = asyncio.create_task(_idle_sweeper(state), name="idle-sweeper")

    try:
        while True:
            msg = await _read_zed_line(reader)
            if msg is None:
                break
            try:
                await _handle_zed_message(state, msg)
            except SystemExit:
                raise
            except Exception:
                logger.exception(
                    "error handling zed message: method=%s id=%s",
                    msg.get("method"), msg.get("id"),
                )
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except (asyncio.CancelledError, Exception):
            pass

        all_handles: list[ChildHandle] = []
        for s in state.sessions.all_sessions():
            if not any(h is s.handle for h in all_handles):
                all_handles.append(s.handle)
        await asyncio.gather(*(shutdown_child(h) for h in all_handles))

    return 0

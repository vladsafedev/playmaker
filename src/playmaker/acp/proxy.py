"""ACP proxy event loop and forwarding rules (§4, §6).

Single asyncio process. One stdio pair to Zed (stdin/stdout). One child
subprocess per session. Shared state in `ProxyState`, touched only from
event-loop callbacks.

This module implements the forwarding-rules table from
docs/acp-phase1.md §4. Inline comments tag the relevant invariants
(corr-N) so changes can cross-reference the design doc.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

from playmaker.acp.caps import (
    init_error_response,
    rewrite_init_response,
)
from playmaker.acp.child import (
    ChildHandle,
    ChildSpawnError,
    shutdown_child,
    spawn_child,
    stderr_tail_text,
)
from playmaker.acp.session_map import ChildSession, SessionMap

logger = logging.getLogger("playmaker.acp.proxy")


# Default child command for Phase 1. Override via `playmaker acp --child <cmd>`.
DEFAULT_CHILD_CMD: list[str] = [
    "/Users/shulyugin/.nvm/versions/node/v24.1.0/bin/npm",
    "exec",
    "@agentclientprotocol/claude-agent-acp@0.31.3",
]


MsgKind = Literal["request", "response", "notification", "error"]


def classify(msg: dict[str, Any]) -> MsgKind:
    """Discriminated union over JSON-RPC message types (corr-4).

    Pending tables MUST only be touched in {request, response, error}
    branches. Notifications have no `id` and never index into pending.
    """
    if "method" in msg:
        return "request" if "id" in msg else "notification"
    if "result" in msg:
        return "response"
    if "error" in msg:
        return "error"
    raise ValueError(f"unclassifiable jsonrpc message: keys={list(msg.keys())}")


@dataclass
class ProxyState:
    """Shared state for the proxy event loop.

    Single-threaded asyncio — no locks, but every mutation must happen
    inside an event-loop callback (no .run_in_executor work touches
    these fields).
    """

    sessions: SessionMap = field(default_factory=SessionMap)
    # The very first child, spawned during Zed's `initialize`. Reused for
    # the first session/new (corr-17). After that, this slot is None.
    primed_child: ChildHandle | None = None
    # Zed's recorded `initialize` request payload — replayed verbatim
    # when we spawn the next (cold) child for session #2+.
    zed_initialize_request: dict[str, Any] | None = None
    # Pending session-creating requests from Zed (session/new, fork, resume)
    # — keyed by the rewritten child-side jsonrpc id. Maps to the original
    # zed_id and the ChildSession we're about to register.
    pending_session_create: dict[int, _PendingSessionCreate] = field(default_factory=dict)
    # Pending session/close — keyed by rewritten child id; maps to
    # (zed_id, zed_sid) so we drop the mapping on response.
    pending_session_close: dict[int, _PendingSessionClose] = field(default_factory=dict)


@dataclass(frozen=True)
class _PendingSessionCreate:
    zed_id: int
    method: str  # "session/new" | "session/fork" | "session/resume"
    request_session_id: str | None  # for resume: the input sid (compare on response)
    child_session: ChildSession


@dataclass(frozen=True)
class _PendingSessionClose:
    zed_id: int
    zed_sid: str


# ---------- Zed-side I/O helpers ---------------------------------------------


async def _write_zed(msg: dict[str, Any]) -> None:
    """Write one JSON-RPC frame to our stdout (Zed reads it)."""
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
        return {"_malformed": True, "raw": raw.decode("utf-8", errors="replace")}


# ---------- Forwarding: Zed → child ------------------------------------------


async def _handle_zed_message(state: ProxyState, msg: dict[str, Any]) -> None:
    """Top-level dispatch for messages from Zed."""
    if msg.get("_malformed"):
        return  # already logged

    kind = classify(msg)
    method = msg.get("method")

    if kind == "request":
        if method == "initialize":
            await _handle_zed_initialize(state, msg)
            return
        if method == "session/new":
            await _handle_zed_session_new(state, msg)
            return
        if method in ("session/fork", "session/resume"):
            await _handle_zed_session_fork_or_resume(state, msg)
            return
        if method == "session/close":
            await _handle_zed_session_close(state, msg)
            return
        # Generic request that targets an existing session (prompt, load,
        # list, set_mode, etc.). All follow the same pattern: rewrite
        # sessionId via by_zed, allocate child-side id, record in
        # out_to_child, forward.
        await _forward_request_to_child(state, msg)
        return

    if kind == "notification":
        # session/cancel and friends — corr-1, corr-4: never touch pending.
        await _forward_notification_to_child(state, msg)
        return

    if kind == "response":
        # Zed answered a request that originated from child (fs/*,
        # session/request_permission, etc.). Look up out_to_zed.
        await _forward_response_to_child(state, msg)
        return

    if kind == "error":
        # Zed errored on a child-originated request. Same path as response.
        await _forward_response_to_child(state, msg)
        return


async def _handle_zed_initialize(state: ProxyState, msg: dict[str, Any]) -> None:
    """Eager spawn (corr-6, corr-16, corr-17): spawn FIRST child, forward
    Zed's initialize verbatim, take child's response, rewrite agentInfo.

    On failure, send a JSON-RPC error reply to Zed (corr-16) and exit.
    """
    zed_id = msg["id"]
    state.zed_initialize_request = msg

    try:
        handle = await spawn_child(DEFAULT_CHILD_CMD)
    except ChildSpawnError as exc:
        await _write_zed(init_error_response(zed_id, str(exc)))
        # Without a child, there's nothing else for the proxy to do.
        raise SystemExit(2)

    # Forward Zed's initialize verbatim to child. (clientCapabilities,
    # protocolVersion, clientInfo all flow through unchanged.)
    try:
        await handle.send(msg)
        child_response = await handle.recv()
    except Exception as exc:  # pragma: no cover - defensive
        await _write_zed(init_error_response(zed_id, f"transport error: {exc}"))
        await shutdown_child(handle)
        raise SystemExit(2) from exc

    if child_response is None:
        tail = stderr_tail_text(handle)
        await _write_zed(init_error_response(zed_id, f"child exited at init; stderr: {tail}"))
        await shutdown_child(handle)
        raise SystemExit(2)

    if classify(child_response) == "error" or "result" not in child_response:
        # Forward error verbatim with our id (Zed expected zed_id, child
        # used the same since we forwarded id-as-is in initialize).
        child_response.setdefault("id", zed_id)
        child_response["id"] = zed_id
        await _write_zed(child_response)
        await shutdown_child(handle)
        raise SystemExit(2)

    # Success. Rewrite agentInfo and respond to Zed.
    response = rewrite_init_response(child_response)
    response["id"] = zed_id
    await _write_zed(response)
    state.primed_child = handle


async def _handle_zed_session_new(state: ProxyState, msg: dict[str, Any]) -> None:
    """session/new: spawn or reuse a child, forward, register mapping on response."""
    zed_id = msg["id"]

    if state.primed_child is not None:
        # corr-17: reuse the init-time child for the first session/new.
        handle = state.primed_child
        state.primed_child = None
    else:
        try:
            handle = await spawn_child(DEFAULT_CHILD_CMD)
        except ChildSpawnError as exc:
            await _write_zed(_jsonrpc_error(zed_id, f"child spawn failed: {exc}"))
            return
        # Replay Zed's initialize to the new child synchronously (BEFORE
        # any drain task starts — otherwise the task would race us on
        # handle.proc.stdout.readline()).
        if state.zed_initialize_request is not None:
            await handle.send(state.zed_initialize_request)
            init_resp = await handle.recv()
            if init_resp is None:
                tail = stderr_tail_text(handle)
                await _write_zed(_jsonrpc_error(zed_id, f"child died during init; stderr: {tail}"))
                await shutdown_child(handle)
                return
            # We don't forward this init response back to Zed — Zed already
            # got initialize response from the primed child.

    # Now forward session/new to the (initialized) child.
    child_id = handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    # corr-5: mcpServers stays as a list — Phase 3 may prepend playmaker's own MCP here.
    params = dict(forwarded.get("params") or {})
    if "mcpServers" in params:
        params["mcpServers"] = list(params["mcpServers"])
    forwarded["params"] = params

    # We don't have a ChildSid yet — we'll learn it from the response.
    placeholder_session = ChildSession(child_sid="<pending>", handle=handle)
    state.pending_session_create[child_id] = _PendingSessionCreate(
        zed_id=zed_id,
        method="session/new",
        request_session_id=None,
        child_session=placeholder_session,
    )
    await handle.send(forwarded)

    # Start the single drain task for this child (idempotent).
    _start_drain_once(state, handle)


async def _handle_zed_session_fork_or_resume(
    state: ProxyState, msg: dict[str, Any]
) -> None:
    """fork/resume: forward, register new mapping on response if child returned a new sid."""
    zed_id = msg["id"]
    zed_sid = (msg.get("params") or {}).get("sessionId")
    if zed_sid is None:
        await _write_zed(_jsonrpc_error(zed_id, "missing sessionId in fork/resume"))
        return
    session = state.sessions.by_zed(zed_sid)
    if session is None:
        await _write_zed(_jsonrpc_error(zed_id, f"unknown sessionId: {zed_sid}"))
        return

    handle = session.handle
    child_id = handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    params = dict(forwarded.get("params") or {})
    params["sessionId"] = session.child_sid
    forwarded["params"] = params

    state.pending_session_create[child_id] = _PendingSessionCreate(
        zed_id=zed_id,
        method=msg["method"],
        request_session_id=session.child_sid,  # for resume sid-equality check
        child_session=session,  # we'll mint new only if response.sid differs
    )
    await handle.send(forwarded)


async def _handle_zed_session_close(state: ProxyState, msg: dict[str, Any]) -> None:
    """session/close: forward, drop mapping on response (corr-12)."""
    zed_id = msg["id"]
    zed_sid = (msg.get("params") or {}).get("sessionId")
    if zed_sid is None:
        await _write_zed(_jsonrpc_error(zed_id, "missing sessionId in close"))
        return
    session = state.sessions.by_zed(zed_sid)
    if session is None:
        await _write_zed(_jsonrpc_error(zed_id, f"unknown sessionId: {zed_sid}"))
        return

    handle = session.handle
    child_id = handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    params = dict(forwarded.get("params") or {})
    params["sessionId"] = session.child_sid
    forwarded["params"] = params

    state.pending_session_close[child_id] = _PendingSessionClose(
        zed_id=zed_id, zed_sid=zed_sid
    )
    session.closed = True
    await handle.send(forwarded)


async def _forward_request_to_child(state: ProxyState, msg: dict[str, Any]) -> None:
    """Generic request: rewrite sid, allocate child-side id, record pending."""
    zed_id = msg["id"]
    params = msg.get("params") or {}
    zed_sid = params.get("sessionId")
    if zed_sid is None:
        await _write_zed(_jsonrpc_error(zed_id, f"missing sessionId in {msg.get('method')!r}"))
        return
    session = state.sessions.by_zed(zed_sid)
    if session is None:
        await _write_zed(_jsonrpc_error(zed_id, f"unknown sessionId: {zed_sid}"))
        return

    child_id = session.handle.alloc_request_id()
    forwarded = dict(msg)
    forwarded["id"] = child_id
    new_params = dict(params)
    new_params["sessionId"] = session.child_sid
    forwarded["params"] = new_params

    # corr-15: multiple in-flight prompts per session are valid.
    session.pending.record_zed_request(child_id, zed_id=zed_id, method=msg.get("method", "?"))
    await session.handle.send(forwarded)


async def _forward_notification_to_child(state: ProxyState, msg: dict[str, Any]) -> None:
    """Notification (no id): rewrite sid only (corr-1, corr-4). No pending changes."""
    params = msg.get("params") or {}
    zed_sid = params.get("sessionId")
    if zed_sid is None:
        # No sid — broadcast or unknown notification. Drop with log.
        logger.warning("notification without sessionId: method=%s", msg.get("method"))
        return
    session = state.sessions.by_zed(zed_sid)
    if session is None:
        logger.warning("notification for unknown sessionId: %s", zed_sid)
        return

    forwarded = dict(msg)
    new_params = dict(params)
    new_params["sessionId"] = session.child_sid
    forwarded["params"] = new_params
    await session.handle.send(forwarded)


async def _forward_response_to_child(state: ProxyState, msg: dict[str, Any]) -> None:
    """Zed answered child's request (fs/*, request_permission, etc.).

    Look up out_to_zed across ALL sessions — Zed-side ids are unique per
    session but not globally; in practice we keyed pending by the
    rewritten zed-side id which we allocated and is unique enough that
    we can scan, but cleaner: maintain a global zed_id → ChildSession
    index. For Phase 1 we scan since the count of sessions is small.
    """
    zed_id = msg["id"]
    for session in state.sessions.all_sessions():
        pending = session.pending.take_child_request(zed_id)
        if pending is None:
            continue
        # Found. Rewrite id back to child's original.
        forwarded = dict(msg)
        forwarded["id"] = pending.child_id
        await session.handle.send(forwarded)
        return
    logger.warning("response from Zed with no pending child-side request: id=%s", zed_id)


# ---------- Child stdout draining --------------------------------------------


def _start_drain_once(
    state: ProxyState, handle: ChildHandle, *, owner_session: ChildSession | None = None
) -> None:
    """Start the single stdout-drain coroutine for `handle`. Idempotent.

    The `_draining` flag is set SYNCHRONOUSLY (before create_task) so that a
    second caller within the same event-loop tick sees True and bails out.
    Without this, two tasks could end up calling readline() on the same
    pipe concurrently → asyncio raises RuntimeError("readuntil called while
    another coroutine is already waiting").
    """
    if getattr(handle, "_draining", False):
        return
    handle._draining = True  # type: ignore[attr-defined]
    asyncio.create_task(
        _drain_child_stdout(state, handle, owner_session=owner_session),
        name=f"child-stdout-{handle.proc.pid}",
    )


async def _drain_child_stdout(
    state: ProxyState,
    handle: ChildHandle,
    *,
    owner_session: ChildSession | None,
) -> None:
    """Read every JSON-RPC frame from child stdout and dispatch.

    On EOF: child died. Synthesize errors for any out_to_child entries
    we own, drop out_to_zed entries (corr-3).

    `_draining` flag is set by the caller (`_start_drain_once`) before
    this coroutine runs — DO NOT set it here, that would re-introduce
    the race window.
    """
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

    # 1) Responses to session-creating requests (session/new/fork/resume).
    if kind in ("response", "error"):
        msg_id = msg.get("id")
        if isinstance(msg_id, int):
            create = state.pending_session_create.pop(msg_id, None)
            if create is not None:
                await _finalize_session_create(state, handle, msg, create)
                return
            close = state.pending_session_close.pop(msg_id, None)
            if close is not None:
                await _finalize_session_close(state, msg, close)
                return

    # 2) Other responses: look up via per-session out_to_child.
    if kind in ("response", "error"):
        msg_id = msg.get("id")
        if isinstance(msg_id, int):
            for session in state.sessions.all_sessions():
                if session.handle is not handle:
                    continue
                pending = session.pending.take_zed_request(msg_id)
                if pending is None:
                    continue
                forwarded = dict(msg)
                forwarded["id"] = pending.zed_id
                await _write_zed(forwarded)
                return
            logger.warning("response from child with no matching pending: id=%s", msg_id)
            return

    # 3) Requests from child (fs/*, session/request_permission).
    if kind == "request":
        await _forward_request_to_zed(state, handle, msg)
        return

    # 4) Notifications from child (session/update, etc.) — corr-8: byte-for-byte.
    if kind == "notification":
        await _forward_notification_to_zed(state, handle, msg)
        return


async def _finalize_session_create(
    state: ProxyState,
    handle: ChildHandle,
    response: dict[str, Any],
    create: _PendingSessionCreate,
) -> None:
    """Process response to session/new/fork/resume: register mapping (or not)."""
    if "error" in response:
        # Forward error verbatim with the original Zed id.
        forwarded = dict(response)
        forwarded["id"] = create.zed_id
        await _write_zed(forwarded)
        return

    result = response.get("result") or {}
    new_child_sid = result.get("sessionId")
    if not new_child_sid:
        forwarded = _jsonrpc_error(create.zed_id, "child returned no sessionId")
        await _write_zed(forwarded)
        return

    if create.method == "session/resume" and create.request_session_id == new_child_sid:
        # Resume reused the same sid — the existing mapping still applies.
        # Reuse the existing zed_sid for the response.
        zed_sid = state.sessions.by_child(new_child_sid)
        if zed_sid is None:
            # Should be impossible — mapping must exist for an existing session.
            forwarded = _jsonrpc_error(create.zed_id, "internal: lost mapping on resume")
            await _write_zed(forwarded)
            return
        zed_sid_str = zed_sid[0]
    else:
        # New sid → new mapping.
        zed_sid_str = SessionMap.mint_zed_sid()
        # If the placeholder ChildSession has child_sid="<pending>",
        # update it; otherwise create a fresh one (fork from existing).
        if create.child_session.child_sid == "<pending>":
            create.child_session.child_sid = new_child_sid
            state.sessions.register(zed_sid=zed_sid_str, session=create.child_session)
        else:
            new_session = ChildSession(child_sid=new_child_sid, handle=handle)
            state.sessions.register(zed_sid=zed_sid_str, session=new_session)

    # Rewrite response.result.sessionId → zed_sid, forward to Zed.
    forwarded = dict(response)
    forwarded["id"] = create.zed_id
    new_result = dict(result)
    new_result["sessionId"] = zed_sid_str
    forwarded["result"] = new_result
    await _write_zed(forwarded)


async def _finalize_session_close(
    state: ProxyState,
    response: dict[str, Any],
    close: _PendingSessionClose,
) -> None:
    """Process response to session/close: drop mapping (corr-12)."""
    forwarded = dict(response)
    forwarded["id"] = close.zed_id
    await _write_zed(forwarded)
    state.sessions.unregister(close.zed_sid)


async def _forward_request_to_zed(
    state: ProxyState, handle: ChildHandle, msg: dict[str, Any]
) -> None:
    """Child-originated request (fs/read_text_file, etc.) → Zed.

    Rewrite sessionId child→zed; allocate zed-side id; record out_to_zed.
    """
    child_id = msg.get("id")
    if not isinstance(child_id, int):
        logger.warning("child request has no integer id: %r", msg.get("method"))
        return

    params = msg.get("params") or {}
    child_sid = params.get("sessionId")
    target_session: ChildSession | None = None
    target_zed_sid: str | None = None
    if child_sid is not None:
        found = state.sessions.by_child(child_sid)
        if found is not None:
            target_zed_sid, target_session = found

    if target_session is None or target_session.handle is not handle:
        logger.warning("child request for unknown sessionId: %s", child_sid)
        return

    # Allocate a zed-side id. We use the child id as-is; ids are
    # per-direction and not required to be globally unique. This
    # avoids needing a separate counter for our zed-side requests.
    zed_id = child_id
    target_session.pending.record_child_request(
        zed_id, child_id=child_id, method=msg.get("method", "?")
    )

    forwarded = dict(msg)
    forwarded["id"] = zed_id
    new_params = dict(params)
    new_params["sessionId"] = target_zed_sid
    forwarded["params"] = new_params
    await _write_zed(forwarded)


async def _forward_notification_to_zed(
    state: ProxyState, handle: ChildHandle, msg: dict[str, Any]
) -> None:
    """Child notification (session/update) → Zed (corr-8: byte-for-byte)."""
    params = msg.get("params") or {}
    child_sid = params.get("sessionId")
    if child_sid is None:
        # A notification without sessionId — pass through as-is.
        await _write_zed(msg)
        return
    found = state.sessions.by_child(child_sid)
    if found is None:
        logger.warning("notification for unknown child sessionId: %s", child_sid)
        return
    zed_sid, _session = found

    forwarded = dict(msg)
    new_params = dict(params)
    new_params["sessionId"] = zed_sid
    forwarded["params"] = new_params
    await _write_zed(forwarded)


# ---------- Death and shutdown -----------------------------------------------


async def _on_child_death(state: ProxyState, handle: ChildHandle) -> None:
    """Child stdout EOF: synthesize errors and drop sessions (corr-3)."""
    affected: list[ChildSession] = [
        s for s in state.sessions.all_sessions() if s.handle is handle
    ]
    for session in affected:
        # Synthesize errors for outstanding Zed requests.
        for pending in session.pending.drain_outstanding_zed_requests():
            await _write_zed(
                _jsonrpc_error(pending.zed_id, f"child died (method={pending.method})")
            )
        # Drop child-originated pending — Zed's eventual responses to
        # these will be ignored by _forward_response_to_child since the
        # session won't be in the map anymore.
        session.pending.drain_outstanding_child_requests()
        # Drop pending session-creating requests targeting this child.
        for pid_, create in list(state.pending_session_create.items()):
            if create.child_session.handle is handle:
                await _write_zed(_jsonrpc_error(create.zed_id, "child died before session/new completed"))
                state.pending_session_create.pop(pid_, None)
        for pid_, close in list(state.pending_session_close.items()):
            sess = state.sessions.by_zed(close.zed_sid)
            if sess is not None and sess.handle is handle:
                await _write_zed(_jsonrpc_error(close.zed_id, "child died before session/close completed"))
                state.pending_session_close.pop(pid_, None)
        state.sessions.unregister(_zed_sid_for(state, session))

    # Phase-2 policy: do NOT auto-respawn. Logged for operator awareness.
    logger.warning("child pid=%s died; %d session(s) affected", handle.proc.pid, len(affected))


def _zed_sid_for(state: ProxyState, session: ChildSession) -> str:
    """Reverse-lookup zed_sid given ChildSession (used in shutdown only)."""
    found = state.sessions.by_child(session.child_sid)
    return found[0] if found else ""


def _stdout_drain_running(handle: ChildHandle) -> bool:
    return getattr(handle, "_draining", False)


def _jsonrpc_error(zed_id: int, message: str, code: int = -32000) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": zed_id, "error": {"code": code, "message": message}}


# ---------- Top-level entry --------------------------------------------------


async def run_proxy(child_cmd: list[str] | None = None) -> int:
    """Main async entrypoint. Read from stdin, pump to/from children.

    Returns process exit code.
    """
    if child_cmd is not None:
        # Mutate module-level default — Phase 1 runs one child kind at a time.
        global DEFAULT_CHILD_CMD
        DEFAULT_CHILD_CMD = child_cmd

    state = ProxyState()
    loop = asyncio.get_running_loop()

    # Wrap stdin with an asyncio StreamReader.
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    try:
        while True:
            msg = await _read_zed_line(reader)
            if msg is None:
                break  # Zed disconnected
            try:
                await _handle_zed_message(state, msg)
            except SystemExit:
                # Initialize-time fatal error — propagate to caller for clean exit.
                raise
            except Exception:
                logger.exception("error handling zed message: method=%s id=%s",
                                 msg.get("method"), msg.get("id"))
    finally:
        # corr-7 / §7: SIGTERM all children, 3s grace, SIGKILL.
        # ChildHandle is a mutable dataclass (not hashable); dedup by identity.
        all_handles: list[ChildHandle] = []
        if state.primed_child is not None:
            all_handles.append(state.primed_child)
        for s in state.sessions.all_sessions():
            if not any(h is s.handle for h in all_handles):
                all_handles.append(s.handle)
        await asyncio.gather(*(shutdown_child(h) for h in all_handles))

    return 0

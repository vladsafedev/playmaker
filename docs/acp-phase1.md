# ACP middleware — Phase 1 design

`playmaker` registers itself in Zed's `agent_servers` as an ACP-speaking
process. It spawns real ACP children (`claude-acp` in Phase 1, others in
Phase 2) and proxies JSON-RPC traffic in both directions, rewriting
`sessionId` and `id` so live `session/update` notifications flow naturally
to Zed without inject-APIs.

```
┌──────┐  ACP    ┌────────────┐  ACP    ┌────────────┐
│ Zed  │ ──────▶ │ playmaker  │ ──────▶ │ claude-acp │
│      │ ◀────── │ (proxy)    │ ◀────── │            │
└──────┘         └────────────┘         └────────────┘
```

Phase 1 scope: single child = `claude-acp`. Phase 2: routing across
`claude-acp`/`codex-acp`/`gemini`. Phase 3: mutations (RAG-as-MCP
injection, mid-stream logging, persistent history in `state.db`).

## Invariants (1-17)

These are written above the relevant fields/methods in code. Listed here
for quick lookup. Numbering matches comment tags in source.

1. **Cancel is a notification.** Forward `session/cancel` Zed→child
   rewriting only `sessionId`. Mapping is NOT cleared. Trailing
   `session/update`'s from child after cancel are valid and must pass
   through up to the final `session/prompt` response with
   `stopReason: "cancelled"`. Lifetime ends only via close, child death,
   or playmaker shutdown.
2. **Mapping lifetime depends on agent caps.** If agent declares
   `sessionCapabilities.close: {}` (both Claude and Codex do), Zed will
   send `session/close`. Proxy clears the mapping on the response.
   Otherwise mapping lives until child death OR playmaker shutdown.
3. **Symmetric pending tables.** Two dicts keyed by jsonrpc id:
   - `out_to_child[child_id] = PendingZedReq{zed_id, method}` — Zed sent
     us a request, we forwarded a (rewritten) request to child, awaiting
     reply.
   - `out_to_zed[zed_id] = PendingChildReq{child_id, method}` — child
     sent us a request, we forwarded to Zed, awaiting reply.
   On child death:
   - For every `out_to_child`: synthesize JSON-RPC error response back to
     Zed with the original `zed_id`. Zed is blocking on this id.
   - For every `out_to_zed`: drop. Log warning. When Zed's response
     eventually arrives, drop it too — no one to forward to.
4. **Discriminated union in demux.** Classify every incoming message into
   `{request, response, notification, error}` BEFORE touching pending
   tables. Notifications (`session/cancel`, `session/update`) have no
   `id` field — never `pending[msg["id"]] = ...` without classify().
5. **mcpServers is mutable forward.** `session/new.params.mcpServers` is
   forwarded as-is in MVP, but stored as a mutable `list` in code so
   Phase 3 can `mcpServers.insert(0, PLAYMAKER_OWN_MCP)` cleanly.
6. **Capabilities = passthrough with name override.** On Zed's
   `initialize`, eagerly spawn child, forward Zed's request to child
   verbatim, take child's response, rewrite ONLY
   `agentInfo.name → "playmaker"` and `agentInfo.title → "Playmaker
   (Claude proxy)"`. Capabilities, `_meta`, version, authMethods —
   untouched. Self-tunes across child versions.
7. **Routing not in MVP.** Single child = `claude-acp`. Multi-child via
   modes deferred to Phase 2.
8. **session/update forwarded byte-for-byte.** Never normalize between
   Claude/Codex/Gemini formats — Zed's renderer expects raw shapes per
   agent. Rewrite ONLY `sessionId`.
9. **Claude permission flow uses internal http-MCP loopback.** Permission
   prompts appear as ordinary `tool_call` with
   `name="mcp__acpPermission__permission"`, NOT native
   `session/request_permission`. Proxy MUST forward as-is; normalizing
   into `session/request_permission` breaks Zed's permission UI. The
   http-MCP traffic itself flows over a separate loopback TCP port,
   invisible to our stdio pipes.
10. **Codex puts model selection under `models` and `configOptions`** in
    `session/new` response, not `SessionModeState`. Codex-specific UI
    extension. Forward as-is.
11. **Cancel does not end the session.** After
    `stopReason: "cancelled"` the same `sessionId` stays alive — Zed can
    send a fresh `session/prompt`. Cancel is per-turn, not per-session.
12. **Codex sends `session/close` even on single-thread open.** First
    observed in 194728 trace: `new → close → new → prompt`. Triggered by
    Zed UI logic (open new thread implies closing the placeholder one).
    Proxy must handle close as a normal request (rewrite, forward, clear
    mapping on response).
13. **First-spawn npm cold-start.** `npm exec
    @agentclientprotocol/claude-agent-acp@0.31.3` on uncached system
    downloads ~30MB and may exceed Zed's spawn timeout. Stderr will
    show "package not found, will be installed". Out-of-MVP mitigation:
    pre-warm cache at install (`npm exec ... --version` once).
14. **Per-agent caps differ materially.** Phase 1 sidesteps this by
    being single-child. Phase 2 (multi-child) needs per-mode dynamic
    caps; not solved here.
15. **promptQueueing implies N>1 pending prompts per session.**
    `_meta.claudeCode.promptQueueing: true` lets Zed send a new
    `session/prompt` while previous is in-flight. `out_to_child` may
    contain multiple entries with `method="session/prompt"` for the
    same `sessionId`. Pending tables are keyed by jsonrpc id (unique),
    so structurally fine — but no code path may assume "one pending
    prompt per session". Don't store a `current_prompt` field on
    `ChildSession`.
16. **Eager-spawn failure surface.** Eager spawn (corr-6) means child
    failures hit during Zed's initialize phase. If child spawn fails OR
    child fails its own initialize, we MUST send a JSON-RPC error
    response to Zed's initialize id. Include child stderr tail in
    `error.message` for diagnostics. Without this, Zed shows a silent
    "agent failed to initialize" with no clue.
17. **First child reuse for first session/new.** The child spawned for
    Zed's initialize is reused for the first `session/new` (it's
    already initialized; spawning a second child to immediately
    discard the first wastes 2-3s). Subsequent `session/new` spawn
    fresh children. Slight asymmetry: session #1 has a child that
    processed `initialize` first, sessions #2+ have cold children.
    TODO: if golden traces show observable divergence between session
    #1 and #2 responses, switch to a "bookkeeping child" pattern
    (dedicated init-only child, all sessions get fresh children).

## §1 Initialize handshake

```
Zed                              playmaker                              claude-acp
 │ initialize ────────────────▶ │  spawn child                           │
 │                               │  ────── initialize (verbatim) ──────▶ │
 │                               │  ◀───── initialize response ───────── │
 │                               │  rewrite agentInfo.{name,title}       │
 │ ◀── initialize response ───── │                                       │
```

If spawn or child initialize fails: respond JSON-RPC `error` to Zed's
initialize id with `code: -32000`, `message: "playmaker: child failed: <stderr tail>"`.
Then exit nonzero. Zed will display the error and not retry within the same
process.

## §2 Pending tables

```python
@dataclass
class PendingZedReq:
    zed_id: int
    method: str
    sent_at: float

@dataclass
class PendingChildReq:
    child_id: int
    method: str
    sent_at: float

class PendingTables:
    out_to_child: dict[int, PendingZedReq]   # rewritten_id → (zed original)
    out_to_zed:   dict[int, PendingChildReq] # rewritten_id → (child original)
```

One PendingTables instance per ChildSession (per-session id-allocation
namespaces don't collide).

## §3 Session map

```python
ZedSid = str
ChildSid = str

@dataclass
class ChildSession:
    child_sid: ChildSid
    handle: ChildHandle
    pending: PendingTables
    closed: bool = False

class SessionMap:
    by_zed:   dict[ZedSid, ChildSession]
    by_child: dict[ChildSid, ZedSid]
```

`session/fork` and `session/resume` create new mappings on response
(extract new child_sid from response, mint zed_sid, register both
directions, rewrite response).

## §4 Forwarding rules

| Direction | Method | id rewrite | sessionId rewrite | Creates mapping? | Notes |
|---|---|---|---|---|---|
| Z→C | `initialize` | yes | n/a | n/a | only first ever; reply via §1 |
| Z→C | `session/new` | yes | n/a | YES (on response) | mint zed_sid |
| Z→C | `session/fork` | yes | yes | YES (on response) | response carries new child_sid |
| Z→C | `session/resume` | yes | yes | conditional | new sid only if response.sid ≠ request.sid |
| Z→C | `session/load` | yes | yes | NO | replay updates flow through existing map |
| Z→C | `session/list` | yes | n/a | NO | rewrite each child sid in response.list to known zed sid; unknown sids dropped (Phase 1 only sees its own children's sids) |
| Z→C | `session/close` | yes | yes | clears on response | record in pending; on response, drop from map |
| Z→C | `session/prompt` | yes | yes | NO | record in `out_to_child` |
| Z→C | `session/cancel` (notif) | n/a | yes | NO | corr-1, NEVER touch pending |
| Z→C | resp to `fs/*`, `session/request_permission` | yes (via `out_to_zed`) | n/a | NO | unwrap zed_id → child_id |
| C→Z | `session/update` (notif) | n/a | yes | NO | byte-for-byte (corr-8) |
| C→Z | `fs/read_text_file`/`write_text_file` (req) | yes | yes | record `out_to_zed` |
| C→Z | `session/request_permission` (req) | yes | yes | record `out_to_zed`; not seen in claude-acp due to corr-9 |
| C→Z | resp to `session/prompt`/`new`/etc | yes (via `out_to_child`) | n/a | NO | retrieve original zed_id |

Phase-2 TODOs explicitly tagged in code: `session/list` aggregation across
children, `session/fork` per-child child mapping.

## §5 Process model

```
playmaker acp (single asyncio process)
  │
  ├── stdin reader (Zed → us): line-buffered JSON-RPC frames
  ├── stdout writer (us → Zed): line-buffered
  │
  └── child sessions:
        ChildSession(zed_sid="Z1") ─→ ChildHandle(pid, stdin, stdout, stderr-task)
        ChildSession(zed_sid="Z2") ─→ ChildHandle(pid, stdin, stdout, stderr-task)
```

Single asyncio event loop. Three task families per child: stdin-writer,
stdout-reader, stderr-tee-to-our-log. Plus two top-level tasks: zed-stdin
reader, zed-stdout writer (multiplexing all children).

Shared state (SessionMap, per-session PendingTables) lives in a single
`ProxyState` dataclass; touched only from event-loop callbacks (no
threading).

## §6 Lifecycle

```
playmaker acp start
  ├── stdin reader starts
  ├── Zed's initialize arrives
  │   ├── spawn FIRST child (claude-acp)
  │   ├── forward Zed's initialize verbatim to child
  │   ├── await child's initialize response
  │   ├── rewrite agentInfo.{name,title}, send to Zed
  │   └── (child remains, will be reused for first session/new — corr-17)
  │   ── on spawn/init failure: synth JSON-RPC error to Zed (corr-16), exit
  │
  ├── session/new from Zed
  │   ├── if first call: use already-initialized child
  │   │   else: spawn new child + forward initialize first (Zed's recorded one)
  │   ├── forward session/new to child
  │   ├── on response: extract child_sid, mint zed_sid, register both directions
  │   └── rewrite response.sessionId → zed_sid, send to Zed
  │
  ├── session/prompt from Zed
  │   ├── classify (corr-4), look up child via by_zed
  │   ├── rewrite sessionId, allocate new child-side jsonrpc id
  │   ├── record out_to_child[child_id] = PendingZedReq(zed_id, "session/prompt")
  │   └── forward to child stdin
  │
  ├── session/update from child (notification)
  │   ├── classify (corr-4): notif → no pending touch
  │   ├── rewrite sessionId via by_child
  │   └── forward to Zed stdout (byte-for-byte content; corr-8)
  │
  ├── session/cancel from Zed (notification)
  │   └── forward sessionId-rewritten; no pending change (corr-1)
  │
  ├── session/close from Zed
  │   ├── forward, await response
  │   └── on response: drop session from SessionMap; clear pending
  │
  ├── child unexpected death (stdout EOF)
  │   ├── for entries in out_to_child: synth error to Zed (corr-3)
  │   ├── for entries in out_to_zed: drop (corr-3)
  │   └── log; do NOT auto-respawn (Phase-2 policy decision)
  │
  └── stdin EOF (Zed disconnected)
      ├── for each child: SIGTERM, wait 3s, SIGKILL on timeout (§7)
      └── exit 0
```

## §7 Graceful shutdown

```python
async def shutdown_child(handle: ChildHandle, timeout: float = 3.0) -> None:
    if handle.proc.returncode is not None:
        return
    handle.proc.terminate()  # SIGTERM
    try:
        await asyncio.wait_for(handle.proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        handle.proc.kill()   # SIGKILL
        await handle.proc.wait()
```

Called from: stdin EOF handler (all children, parallel), per-session
`session/close` response (one child), and unexpected child exit handler
(no-op since dead).

## §8 What is NOT in MVP (Phase 1)

- Multi-child routing (Phase 2).
- `session/load` replay from `state.db` (rely on Claude's native
  `loadSession`).
- Mutations in `session/update` (Phase 3 RAG injection).
- `mcpServers` augmentation (forward as-is; Phase 3 hook reserved as
  mutable list — corr-5).
- Session modes / dynamic capability switching (Phase 2 multi-child).
- Auth proxying (Claude pre-authenticated; login flow forward'ить
  unnecessary).
- Auto-respawn after child death (Phase-2 policy).
- npm cache pre-warming (corr-13 — out-of-band install hook).
- Bookkeeping child pattern (corr-17 — only if golden traces show
  divergence between session #1 and session #2).
- `session/list` aggregation across children (Phase 2).

## §9 File structure

```
src/playmaker/acp/
├── __init__.py
├── __main__.py            # `python -m playmaker.acp` for manual debug
├── caps.py                # protocol constants + name-override helper
├── pending.py             # PendingTables + Pending{Zed,Child}Req
├── session_map.py         # SessionMap, ChildSession dataclasses
├── child.py               # ChildHandle: spawn, async stdio, shutdown
├── proxy.py               # main event loop + forwarding rules
└── server.py              # `playmaker acp` Typer subcommand
```

`src/playmaker/cli.py` registers the new `acp` subcommand via
`server.app`.

## §10 Testing

### Smoke (manual, against real Zed)

1. Register `playmaker` in `~/.config/zed/settings.json` `agent_servers`
   as `type: custom` with command `playmaker acp`.
2. Cmd+Q + reopen Zed.
3. Plus → playmaker → New thread → `say hi`.
4. Verify live `session/update` rendering in Zed UI: chunks appear as
   typed, not all-at-once on completion.
5. Tail `~/acp-logs/playmaker-proxy-<ts>.log` (proxy-side log) — should
   mirror reference traces from `~/acp-logs/claude-20260428-211116.{in,out}.jsonl`
   structurally.

### Golden trace

```python
# tests/test_acp_golden.py
def test_replay_claude_chat(tmp_path):
    # Feed in.jsonl line-by-line to playmaker acp's stdin.
    # Mock child reads our forwarded stdin, replays out.jsonl.
    # Assert: stdout we send to Zed matches expected, with sids/ids
    # rewritten consistently (i.e. there exists a bijection mapping
    # our zed-side sids to fixture's child-side sids that explains
    # every rewrite).
```

Fixtures: `tests/fixtures/claude-211116.{in,out}.jsonl` (copies of
real reference traces).

Validates: id/sid rewriting, message-shape preservation, ordering.
Does NOT validate: concurrency, races under contention, partial-frame
parsing edge cases. Those defer to Phase 2 fuzz tests.

## §11 Open questions / TODO

- **session/resume sid behavior** — does claude-acp return new sid or
  reuse? Empirical test in Phase 1 (no fixture in current traces).
- **session/list cross-child aggregation** — Phase 2 only.
- **`session/list` for unknown sids** — if Zed somehow sends a sid we
  don't know (e.g. from Zed's pre-existing thread store written by
  some other process before playmaker), proxy should respond
  `error: unknown session` rather than silently forward to a random
  child.
- **Bookkeeping child** — see corr-17. Decision deferred.
- **Multi-session deadlock from a sync stdin/stdout driver.** During Phase 1
  development a sync-driver smoke test (subprocess.PIPE write + readline)
  hung on the *second* `session/new`. Real Zed (asyncio client) handled
  4+ concurrent threads through the proxy without issue, so the freeze
  is most likely driver-side pipe buffering, not a proxy bug. Reproducer
  and minimal asyncio driver to confirm — Phase 2 task; if the freeze
  reproduces with an asyncio driver, the proxy has a real second-session
  bug that doesn't manifest under Zed's I/O pattern.

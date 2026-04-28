# ACP Phase 2 — replay-driven, sidebar-aware

Phase 1 built a transparent ACP proxy where the user opens threads through
Zed's Plus-menu. Useful primitives, wrong target. The actual playmaker UX
is **batch dispatch**: `playmaker dispatch claude/codex/gemini --prompt "..."`
spawns a sub-agent detached, writes a row into Zed's `sidebar_threads` so
the thread appears in the sidebar after reload. The user clicks the row;
Zed opens the thread.

Phase 1 left those threads looking frozen — Zed read the agent's session-file
from disk, no live indicator, "Proceed" prompt on incomplete history.

**Phase 2 fixes this.** The proxy becomes a proper agent: serves
`session/load` from playmaker's `state.db`, replays the agent's history
through `session/update` notifications, and on follow-up `session/prompt`
spawns the right child via `handler.resume()` for live continuation.

## What changed vs Phase 1

| | Phase 1 | Phase 2 |
|---|---|---|
| Initialize | spawn child eagerly, forward, name-override response | static caps, no child spawn |
| session/load | forward to child verbatim | serve from `state.db` + replay |
| session/prompt | always Plus-menu path: forward to existing child | dual: existing child OR resume-after-load |
| Child lifecycle | spawn-on-session-new, no eviction | LRU pool N=3, idle timeout 5min |
| Capability calculus | passthrough from child | static — deferred for Phase 3 multi-agent |
| User entry point | Plus-menu (one thread) | sidebar (N threads from dispatch) + Plus-menu still works |

Phase 1 modules (proxy/child/session_map/pending) are reused. Two new
modules added: `caps.py` (rewritten as static), `replay.py` (Turn → ACP
session/update converter).

## §1 The session/load handler

When Zed sends `session/load { sessionId, cwd, mcpServers }`:

1. `state_db.get_session_by_agent_session_id(sessionId)` — sessionId here
   is the AGENT's native sid (claude UUID, codex thread_id, gemini
   sessionId), which is what `playmaker zed.register()` writes into
   `sidebar_threads.session_id`. If the row doesn't exist, return JSON-RPC
   error.
2. `handler.parse_session_file(...)` returns normalized `Turn[]` (Phase 1
   work — same code that powers `playmaker thread <id>`).
3. `replay.turns_to_updates(turns)` yields ACP `update` payloads, one per
   semantic event (user message, agent text, agent thinking, tool_call,
   tool_call_update). Proxy emits each as a `session/update` notification.
4. **No synthesized completions.** If the history shows the agent died
   mid-stream, replay shows that. Zed's "Proceed" UI in that case is
   correct — it's the affordance for resume.
5. Reply to load with `{ sessionId, modes: null, models: null }` (mimics
   claude-acp's response shape; minimal valid).
6. Final `session/update` with `available_commands_update: []` — observed
   in the canonical capture, semantic "ready for new input".

No child spawn happens during load. That's deferred to step (1) of §2.

## §2 The resume-after-load path on session/prompt

The user reads a replayed thread, types "and then?". Zed sends
`session/prompt { sessionId, prompt }`. Proxy:

1. Look up `sessionId` in `SessionMap`. If found → live child already
   wired, just forward the prompt (Plus-menu path).
2. Not found → `state_db.get_session_by_agent_session_id(sessionId)`
   confirms this is a known dispatched thread.
3. `_ensure_pool_capacity()` — if we already have `MAX_CHILDREN=3`, evict
   the LRU (idle longest) child via `shutdown_child` + unregister.
4. `spawn_child(DEFAULT_CHILD_CMD)` — fresh ACP child.
5. Replay Zed's `initialize` to it (we recorded it on Zed's first call) so
   it sees Zed's `clientCapabilities`.
6. Send the child its own `session/load` for this `sessionId` so the child
   restores its in-memory thread state. Drain its replay — Zed already saw
   our replay, drop the duplicate.
7. Register `(sessionId ↔ child_sid=sessionId)` in `SessionMap` — child
   kept the same sid since `session/load` is identity, not creation.
8. Forward the actual prompt → live streaming begins.

Note: this reuses Phase 1's resume-via-handler.resume() **only conceptually**.
The actual mechanism is `handler.parse_session_file` + child's own
`session/load`, because in ACP context we go through ACP, not the agent's
own CLI subcommand.

## §3 LRU pool + idle timeout

`MAX_CHILDREN = 3`, `IDLE_TIMEOUT_SECONDS = 300` (5 min), sweep interval
`60s`. Why these numbers:

- 3 children = a typical coach-pattern flight (one delegated each to
  Claude/Codex/Gemini). Anything beyond is a sign the user is reading
  history, not running parallel work.
- 5 min idle timeout matches typical "look at one thread for a while" but
  evicts forgotten threads before they accumulate.
- Sweep every 60s — cheap, doesn't need to be precise.

`ChildSession.last_activity` is updated on every forward in either
direction (`session.touch()`). LRU is just the min of `last_activity`.

If an evicted thread receives a new prompt later, the resume-after-load
path simply spawns a fresh child for it. Cost: one cold-start (~2-3s for
claude-acp via npm exec).

## §4 Static capabilities

Declared at initialize, no child needed:

```json
{
  "protocolVersion": 1,
  "agentCapabilities": {
    "loadSession": true,
    "promptCapabilities": {"image": true, "embeddedContext": true},
    "mcpCapabilities": {"http": true, "sse": true},
    "sessionCapabilities": {"close": {}}
  },
  "agentInfo": {"name": "playmaker", "title": "Playmaker", "version": "0.1.0"}
}
```

Trade-off: if a capability is declared and the wrapped child doesn't
support it, we'll error at runtime. Mitigation in MVP: shape mirrors
claude-acp@0.31.3 (the only child wrapped in Phase 2). Phase 3 multi-agent
revisits via per-mode caps.

## §4.5 Live attach (Phase 2.5 — implemented)

If the dispatched sub-agent is still running when the user clicks the
sidebar row, replay alone gives a snapshot, not live progress. Phase 2.5
adds a poll-based file watcher that streams deltas until the dispatch
finishes.

**Mechanism (proxy.py):**

- After initial replay in `_handle_zed_session_load`, re-read the
  state.db row. If `status == "running"`, register a `_Watcher` in
  `ProxyState.watchers[sessionId]` with `emitted_count = len(initial_turns)`
  and spawn `_watch_session_file` as an asyncio task.
- Watcher polls every `WATCHER_POLL_INTERVAL = 0.5s`:
  re-runs `handler.parse_session_file(...)`, compares `len(turns)` vs
  `emitted_count`, emits `session/update` notifications for the delta.
- Read-only access to the session-file. The dispatch process is the sole
  writer. `parse_session_file` already tolerates partial-last-line writes
  via try/except json.JSONDecodeError.
- Termination: when `status` flips to `done`/`failed`/`killed`, the watcher
  does one final flush (waits `WATCHER_TERMINAL_FLUSH_DELAY = 0.5s` to
  catch tail writes), emits remaining delta, exits. The `finally` block
  removes the entry from `state.watchers`.

**Follow-up blocking (MVP simplicity):**

While `sid in state.watchers`, `_handle_zed_session_prompt` returns a
JSON-RPC error explaining that the dispatch is still running and offering
`playmaker kill <id>` as the abort path. This avoids a race where a fresh
resume-after-load child writes to the same session-file as the still-alive
dispatch process.

**Shutdown:**

`run_proxy`'s `finally` cancels every active watcher task before draining
children.

## §5 What is NOT in Phase 2

- **Multi-agent routing.** Phase 3 — only if AI Designer integration
  surfaces a need.
- **mcpServers augmentation.** Phase 3 RAG-as-MCP. Hook is reserved as
  mutable `list(req.mcpServers)` in `_handle_zed_session_new`.
- **session/fork**, **session/list aggregation** — Phase 3.
- **Capability calculus across children** — deferred not deleted. Static
  caps work for single-child Phase 2; multi-agent Phase 3 will need
  per-mode dynamic caps.

## §6 Tests

| Test | What it covers |
|---|---|
| `tests/test_acp_replay.py` (9 cases) | Turn → session/update mapping; canonical capture order; no synthesized completion; thinking-vs-text split |
| `tests/test_acp_smoke.py` (1 case) | Phase 1 regression: Plus-menu session/new with fake child, sid mint, sid rewrite, cancel forward, clean shutdown |
| `tests/test_acp_session_load.py` (2 cases) | End-to-end: temp state.db + fake jsonl + drive proxy via subprocess pipes; assert replay order + load-result shape + available_commands_update; assert error on unknown sid |
| `tests/test_acp_watcher.py` (2 cases) | Phase 2.5: background thread simulates dispatch writing jsonl + flipping status; assert watcher emits delta + terminates on terminal status; assert follow-up blocked with "still running" while watcher active and unblocks after termination |

All 14 tests run as part of CI (`.github/workflows/ci.yml` `Run unit tests` step).

## §7 Open questions / TODO

### Watched (act only if it surfaces)

- **"Proceed" UX correctness.** Hypothesis: Zed renders the right affordance
  based on whether replay ended with assistant-completion or user-input.
  No empirical capture for the unfinished case (skipped to avoid further
  manual testing). If smoke shows "Proceed" appears on cleanly-finished
  threads, the converter needs adjustment — likely a missing terminal
  marker. Fix is local to `replay.py`.
- **Child cold-start on resume-after-load.** ~2-3s to spawn npm exec
  claude-acp. Visible as a brief delay between the user's prompt and the
  first `session/update`. If users complain, pre-warm via `playmaker init`.

### Planned (to be done)

- **Phase 3 multi-agent** when AI Designer integration starts.

## §8 File structure

```
src/playmaker/acp/
├── __init__.py
├── __main__.py            # `python -m playmaker.acp` for manual debug
├── caps.py                # static initialize_response (Phase 2 rewrite)
├── child.py               # async stdio + spawn + shutdown helper
├── pending.py             # PendingTables — symmetric request bookkeeping
├── proxy.py               # event loop + handlers (load/prompt/new/close/...)
├── replay.py              # Turn[] -> ACP session/update converter (NEW)
├── server.py              # `playmaker acp` Typer subcommand
└── session_map.py         # SessionMap + ChildSession.last_activity (touched for LRU)
```

The Phase 1 `docs/acp-phase1.md` stays — it documents the foundations
(invariants 1-17, forwarding rules) that Phase 2 reuses verbatim. Phase 2
describes the *new* behaviour layered on top.

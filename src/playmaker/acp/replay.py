"""Turn[] -> ACP session/update converter for `session/load` replay.

Phase 2 path B: when Zed sends `session/load` for a sessionId that lives in
playmaker's state.db, we read the agent's native session-file via
`handler.parse_session_file(...)` (which gives us normalized Turn[]) and
emit one or more `session/update` notifications per turn before responding
to the load request.

Mapping (validated against ~/acp-logs/claude-20260428-224329.out.jsonl —
canonical capture of claude-acp's own session/load behaviour):

    Turn(role=user)                     -> 1 update of kind user_message_chunk
    Turn(role=assistant, content=text)  -> 1 update of kind agent_message_chunk
    Turn(role=assistant) with thinking  -> agent_thought_chunk for the thinking
                                           block + agent_message_chunk for text
    Turn(role=assistant, tool_calls=[]) -> 1 update of kind tool_call per call
    Turn(role=tool, tool_results=[])    -> 1 update of kind tool_call_update
                                           per result (matched by tool_use_id)

No synthesized completions, no skipped content. If a Turn shows the agent
gave up halfway, replay shows it gave up halfway — Zed's "Proceed" UI in
that case is correct, and a follow-up session/prompt will trigger our
resume-after-load path (proxy.py).

Note on Claude `Turn` shape from src/playmaker/agents/claude.py:
  - thinking blocks are prepended to .content as "[thinking] <text>", we
    detect the prefix to split into agent_thought_chunk vs agent_message_chunk.
"""

from __future__ import annotations

from typing import Any, Iterator

from playmaker.agents.base import Turn


_THINKING_PREFIX = "[thinking] "


def turns_to_updates(turns: list[Turn]) -> Iterator[dict[str, Any]]:
    """Yield ACP `update` payloads (the inner objects of `session/update`
    notifications) corresponding to each Turn.

    Caller wraps each yielded dict into a JSON-RPC notification:
        {"jsonrpc":"2.0","method":"session/update",
         "params":{"sessionId": <zed-side>, "update": <yielded dict>}}
    """
    for turn in turns:
        yield from _turn_to_updates(turn)


def _turn_to_updates(turn: Turn) -> Iterator[dict[str, Any]]:
    if turn.role == "user":
        if turn.content:
            yield {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": turn.content},
            }
        return

    if turn.role == "assistant":
        # Split content into thinking vs ordinary text. ClaudeHandler stores
        # thinking blocks as "[thinking] <text>" lines mixed with regular text
        # in turn.content (joined by "\n"). Codex/Gemini don't expose thinking
        # in their parse_session_file output, so this affects only Claude.
        if turn.content:
            for line in turn.content.split("\n"):
                if line.startswith(_THINKING_PREFIX):
                    text = line[len(_THINKING_PREFIX) :]
                    yield {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": text},
                    }
                else:
                    yield {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": line},
                    }
        for tc in turn.tool_calls:
            yield {
                "sessionUpdate": "tool_call",
                "toolCallId": tc.get("id") or "",
                "title": str(tc.get("name") or "tool"),
                "kind": "other",
                "status": "pending",
                "rawInput": tc.get("input"),
            }
        return

    if turn.role == "tool":
        for tr in turn.tool_results:
            yield {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tr.get("tool_use_id") or "",
                "status": "completed",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": tr.get("content", "")}}
                ],
            }
        return

    # Unknown role — skip silently. Future schema additions hit this path
    # without crashing the replay.

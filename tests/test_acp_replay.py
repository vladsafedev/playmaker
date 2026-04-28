"""Unit tests for the Turn[] -> ACP session/update converter (replay.py).

Validates the mapping table from docs/acp-phase2.md against in-memory
Turn objects (no JSONL fixture needed — Turn is the normalized format
that all three handlers produce).
"""

from __future__ import annotations

import unittest

from playmaker.acp.replay import turns_to_updates
from playmaker.agents.base import Turn


class ReplayTest(unittest.TestCase):
    def test_user_turn_emits_user_message_chunk(self) -> None:
        turns = [Turn(role="user", content="hello")]
        updates = list(turns_to_updates(turns))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["sessionUpdate"], "user_message_chunk")
        self.assertEqual(updates[0]["content"]["text"], "hello")

    def test_empty_user_turn_is_skipped(self) -> None:
        turns = [Turn(role="user", content="")]
        self.assertEqual(list(turns_to_updates(turns)), [])

    def test_assistant_text_emits_agent_message_chunk(self) -> None:
        turns = [Turn(role="assistant", content="hi")]
        updates = list(turns_to_updates(turns))
        self.assertEqual(updates, [
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "hi"}},
        ])

    def test_assistant_thinking_split_from_text(self) -> None:
        # ClaudeHandler stores thinking blocks as "[thinking] ..." lines mixed
        # with regular text in turn.content; replay must split.
        turns = [Turn(
            role="assistant",
            content="[thinking] reasoning step\nfinal answer",
        )]
        updates = list(turns_to_updates(turns))
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]["sessionUpdate"], "agent_thought_chunk")
        self.assertEqual(updates[0]["content"]["text"], "reasoning step")
        self.assertEqual(updates[1]["sessionUpdate"], "agent_message_chunk")
        self.assertEqual(updates[1]["content"]["text"], "final answer")

    def test_tool_call_emits_pending(self) -> None:
        turns = [Turn(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc_1", "name": "Read", "input": {"path": "x"}}],
        )]
        updates = list(turns_to_updates(turns))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["sessionUpdate"], "tool_call")
        self.assertEqual(updates[0]["toolCallId"], "tc_1")
        self.assertEqual(updates[0]["title"], "Read")
        self.assertEqual(updates[0]["status"], "pending")

    def test_tool_role_emits_completed_update(self) -> None:
        turns = [Turn(
            role="tool",
            content="",
            tool_results=[{"tool_use_id": "tc_1", "content": "file contents"}],
        )]
        updates = list(turns_to_updates(turns))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["sessionUpdate"], "tool_call_update")
        self.assertEqual(updates[0]["toolCallId"], "tc_1")
        self.assertEqual(updates[0]["status"], "completed")

    def test_no_synthesized_completion_for_unfinished_history(self) -> None:
        # If the replay ends with a user turn (sub-agent died mid-prompt),
        # we MUST NOT append a synthetic agent_message_chunk. Zed's UI will
        # render this as an open turn and offer "Proceed" — the correct
        # affordance for resume-after-load.
        turns = [
            Turn(role="user", content="say hi"),
            Turn(role="assistant", content="Hi"),  # partial; no continuation
            Turn(role="user", content="and then what?"),
        ]
        updates = list(turns_to_updates(turns))
        # 3 updates total; last one is a user_message_chunk, not a synthetic
        # agent close.
        self.assertEqual(len(updates), 3)
        self.assertEqual(updates[-1]["sessionUpdate"], "user_message_chunk")

    def test_unknown_role_is_silently_skipped(self) -> None:
        turns = [Turn(role="system", content="bootstrap")]
        self.assertEqual(list(turns_to_updates(turns)), [])

    def test_canonical_sequence_matches_claude_replay_capture(self) -> None:
        # Order from ~/acp-logs/claude-20260428-224329.out.jsonl:
        #   user_message_chunk -> agent_thought_chunk -> tool_call ->
        #   tool_call_update -> agent_message_chunk
        turns = [
            Turn(role="user", content="explain X"),
            Turn(role="assistant",
                 content="[thinking] need to read file",
                 tool_calls=[{"id": "t1", "name": "Read", "input": {"path": "x"}}]),
            Turn(role="tool",
                 content="",
                 tool_results=[{"tool_use_id": "t1", "content": "X is..."}]),
            Turn(role="assistant", content="X is the thing because..."),
        ]
        kinds = [u["sessionUpdate"] for u in turns_to_updates(turns)]
        self.assertEqual(kinds, [
            "user_message_chunk",
            "agent_thought_chunk",
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
        ])


if __name__ == "__main__":
    unittest.main()

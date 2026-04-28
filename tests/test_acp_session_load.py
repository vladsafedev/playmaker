"""End-to-end smoke for the path-B `session/load` flow.

Scenario:
  1. Write a fake claude-style session.jsonl to a temp path.
  2. Insert a row into a TEMP playmaker state.db pointing at that file
     with agent="claude" and a known agent_session_id.
  3. Spawn `playmaker.acp.proxy.run_proxy` in a subprocess against in/out
     pipes (no real Zed).
  4. Send Zed-side messages: initialize -> session/load with the known sid.
  5. Assert the proxy emitted:
        - JSON-RPC initialize response with static playmaker caps,
        - N session/update notifications matching the file's content,
        - JSON-RPC session/load response with {sessionId, modes, models},
        - one final session/update for available_commands_update.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


# Minimal claude-style jsonl content. ClaudeHandler.parse_session_file expects:
#   {"type": "user"|"assistant", "message": {"role":..., "content": str|list}, "timestamp": str}
def make_claude_jsonl(path: Path, sid: str) -> None:
    lines = [
        # user
        {"type": "user", "message": {"role": "user", "content": "say hi"},
         "timestamp": "2026-04-28T22:00:00Z"},
        # assistant text
        {"type": "assistant",
         "message": {
             "role": "assistant",
             "content": [{"type": "text", "text": "Hi"}],
         },
         "timestamp": "2026-04-28T22:00:01Z"},
    ]
    with path.open("w") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


class SessionLoadE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)

        # Override PLAYMAKER_HOME so we don't pollute the user's real ~/.playmaker.
        self.playmaker_home = tmp_path / "playmaker_home"
        self.playmaker_home.mkdir()

        # Fake session file (claude jsonl).
        self.session_file = tmp_path / "fake-session.jsonl"
        self.agent_session_id = str(uuid.uuid4())
        make_claude_jsonl(self.session_file, self.agent_session_id)

        # Init state.db inside our tmp playmaker home and insert the row.
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "HOME": str(tmp_path),  # so Path("~/.playmaker") resolves under tmp
        }
        self.env = env

        # Init the DB and insert a row using the actual state module so the
        # schema matches.
        init_script = (
            "from playmaker import state as st;"
            "st.init_db();"
            "sid = st.insert_session(agent='claude', prompt='say hi', cwd='/tmp');"
            f"st.update_session(sid, agent_session_id='{self.agent_session_id}', "
            f"  status='done', session_file_path='{self.session_file}', "
            f"  output_path='', exit_code=0)"
        )
        subprocess.run(
            [sys.executable, "-c", init_script], env=env, check=True
        )

        # Spawn the proxy. We don't care about its child command for this
        # test (load doesn't spawn a child) but provide a no-op one.
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "playmaker.acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def tearDown(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self.tmp.cleanup()

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _recv(self, timeout: float = 5.0) -> dict:
        assert self.proc.stdout is not None
        end = time.time() + timeout
        while time.time() < end:
            line = self.proc.stdout.readline()
            if line:
                return json.loads(line)
        raise TimeoutError("proxy stdout timeout")

    def test_load_replays_then_returns_rich_result(self) -> None:
        # initialize
        self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}},
        })
        init = self._recv()
        self.assertEqual(init["id"], 0)
        self.assertEqual(init["result"]["agentInfo"]["name"], "playmaker")

        # session/load
        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "session/load",
            "params": {
                "sessionId": self.agent_session_id,
                "cwd": "/tmp",
                "mcpServers": [],
            },
        })

        # We expect, in order: 2 session/update notifications (user + assistant),
        # then the load response, then one available_commands_update notification.
        msg1 = self._recv()
        self.assertEqual(msg1.get("method"), "session/update")
        self.assertEqual(msg1["params"]["sessionId"], self.agent_session_id)
        self.assertEqual(msg1["params"]["update"]["sessionUpdate"], "user_message_chunk")
        self.assertEqual(msg1["params"]["update"]["content"]["text"], "say hi")

        msg2 = self._recv()
        self.assertEqual(msg2["params"]["update"]["sessionUpdate"], "agent_message_chunk")
        self.assertEqual(msg2["params"]["update"]["content"]["text"], "Hi")

        # Load reply.
        load_resp = self._recv()
        self.assertEqual(load_resp["id"], 1)
        self.assertEqual(load_resp["result"]["sessionId"], self.agent_session_id)
        self.assertIn("modes", load_resp["result"])
        self.assertIn("models", load_resp["result"])

        # available_commands_update afterward.
        cmds = self._recv()
        self.assertEqual(cmds["params"]["update"]["sessionUpdate"], "available_commands_update")
        self.assertEqual(cmds["params"]["update"]["availableCommands"], [])

    def test_load_unknown_session_returns_error(self) -> None:
        self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}},
        })
        self._recv()  # init reply

        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "session/load",
            "params": {"sessionId": "nope-not-in-db", "cwd": "/tmp", "mcpServers": []},
        })
        resp = self._recv()
        self.assertEqual(resp["id"], 1)
        self.assertIn("error", resp)
        self.assertIn("unknown sessionId", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main()

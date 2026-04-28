"""Phase 2.5 — file watcher live attach.

Setup: temp playmaker home + state.db row with status='running' + a fake
claude-style jsonl file. Drive `playmaker.acp` proxy via subprocess pipes.

Background thread simulates the dispatch process: appends new jsonl lines
to the session-file at intervals, then flips status to 'done' in state.db.

Asserts:
  - On session/load: initial replay (pre-existing turns) emitted before result.
  - After result + available_commands_update: watcher emits delta turns
    appended by the simulated dispatch.
  - On status flip to terminal: final flush emits remaining turns.
  - Follow-up session/prompt while watcher active: BLOCKED with explicit error.
  - After watcher terminates: follow-up unblocked (path 2 takes over —
    we don't spawn a child here, just confirm the error is gone and we
    get a different error or branch).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _claude_user(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-04-28T22:00:00Z",
    }


def _claude_assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": "2026-04-28T22:00:01Z",
    }


def _append_lines(path: Path, lines: list[dict]) -> None:
    with path.open("a") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
            fh.flush()


class WatcherE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)

        self.session_file = tmp_path / "fake-session.jsonl"
        self.agent_session_id = str(uuid.uuid4())
        # Pre-existing turns (what initial replay returns).
        _append_lines(self.session_file, [
            _claude_user("start"),
            _claude_assistant_text("first chunk"),
        ])

        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "HOME": str(tmp_path),
        }
        self.env = env

        # Init state.db with status=running.
        init_script = (
            "from playmaker import state as st;"
            "st.init_db();"
            "sid = st.insert_session(agent='claude', prompt='start', cwd='/tmp');"
            f"st.update_session(sid, agent_session_id='{self.agent_session_id}', "
            f"  status='running', session_file_path='{self.session_file}', "
            f"  output_path='', exit_code=0)"
        )
        subprocess.run([sys.executable, "-c", init_script], env=env, check=True)

        self.proc = subprocess.Popen(
            [sys.executable, "-m", "playmaker.acp", "--log-level", "WARNING"],
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

    def _recv(self, timeout: float = 8.0) -> dict:
        assert self.proc.stdout is not None
        end = time.time() + timeout
        while time.time() < end:
            line = self.proc.stdout.readline()
            if line:
                return json.loads(line)
        raise TimeoutError("proxy stdout timeout")

    def _set_status(self, status: str) -> None:
        """Flip state.db.status from a child python; required because the test
        process and the proxy use separate sqlite connections (HOME is set
        per-process)."""
        script = (
            f"from playmaker import state as st;"
            f"row = st.get_session_by_agent_session_id('{self.agent_session_id}');"
            f"st.update_session(row['id'], status='{status}')"
        )
        subprocess.run([sys.executable, "-c", script], env=self.env, check=True)

    def test_watcher_emits_delta_then_terminates(self) -> None:
        # initialize
        self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}},
        })
        self._recv()  # init reply

        # session/load
        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "session/load",
            "params": {
                "sessionId": self.agent_session_id,
                "cwd": "/tmp",
                "mcpServers": [],
            },
        })

        # Initial replay: 2 updates (user "start", assistant "first chunk").
        m1 = self._recv()
        self.assertEqual(m1["params"]["update"]["sessionUpdate"], "user_message_chunk")
        m2 = self._recv()
        self.assertEqual(m2["params"]["update"]["sessionUpdate"], "agent_message_chunk")

        # Load reply.
        load_resp = self._recv()
        self.assertEqual(load_resp["id"], 1)

        # available_commands_update.
        cmds = self._recv()
        self.assertEqual(cmds["params"]["update"]["sessionUpdate"], "available_commands_update")

        # Now simulate dispatch writing more turns in the background, then
        # flipping status to done.
        def writer() -> None:
            time.sleep(0.6)  # past one watcher poll
            _append_lines(self.session_file, [_claude_assistant_text("second chunk")])
            time.sleep(0.6)
            _append_lines(self.session_file, [_claude_assistant_text("third chunk")])
            time.sleep(0.6)
            self._set_status("done")

        t = threading.Thread(target=writer, daemon=True)
        t.start()

        # Watcher should emit "second chunk" and "third chunk" within ~3s.
        delta1 = self._recv(timeout=3.0)
        self.assertEqual(delta1["method"], "session/update")
        self.assertEqual(delta1["params"]["update"]["sessionUpdate"], "agent_message_chunk")
        self.assertEqual(delta1["params"]["update"]["content"]["text"], "second chunk")

        delta2 = self._recv(timeout=3.0)
        self.assertEqual(delta2["params"]["update"]["content"]["text"], "third chunk")

        t.join(timeout=5.0)

        # Watcher does a final flush (0.5s sleep + emit_delta) after seeing
        # terminal status, then exits. Poll the unblock signal by retrying
        # the follow-up — the "still running" message disappears once
        # state.watchers.pop(sid) ran.
        unblocked = False
        next_id = 2
        for attempt in range(8):  # up to ~4s of retries
            self._send({
                "jsonrpc": "2.0", "id": next_id, "method": "session/prompt",
                "params": {
                    "sessionId": self.agent_session_id,
                    "prompt": [{"type": "text", "text": "and then?"}],
                },
            })
            resp = self._recv(timeout=10.0)
            self.assertEqual(resp["id"], next_id)
            self.assertIn("error", resp)
            if "still running" not in resp["error"]["message"]:
                unblocked = True
                break
            next_id += 1
            time.sleep(0.5)
        self.assertTrue(unblocked, f"watcher never released the block; last error: {resp.get('error')}")

    def test_followup_blocked_while_watcher_active(self) -> None:
        self._send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}},
        })
        self._recv()

        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "session/load",
            "params": {
                "sessionId": self.agent_session_id,
                "cwd": "/tmp",
                "mcpServers": [],
            },
        })
        # Drain initial replay + load reply + available_commands_update.
        for _ in range(4):
            self._recv()

        # Watcher is active (status still 'running'). Send follow-up.
        self._send({
            "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
            "params": {
                "sessionId": self.agent_session_id,
                "prompt": [{"type": "text", "text": "follow-up"}],
            },
        })
        resp = self._recv()
        self.assertEqual(resp["id"], 2)
        self.assertIn("error", resp)
        self.assertIn("still running", resp["error"]["message"])
        self.assertIn("playmaker kill", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main()

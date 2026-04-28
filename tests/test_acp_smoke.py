"""Regression smoke for the ACP server.

Phase 2 changes (vs the original Phase 1 smoke):
  - initialize now responds with STATIC playmaker caps (no child spawn),
    so we no longer assert that fake-child's _meta.fake passes through.
  - session/new still spawns a child and forwards verbatim; sid mint and
    rewrite assertions are unchanged.

Asserts:
  - initialize: agentInfo {name="playmaker", title="Playmaker"} + the
    declared static caps (loadSession=true, sessionCapabilities.close).
  - §3      session/new: proxy mints a new zed-side sid, never echoes
            the child's
  - §4      session/update from child: sessionId rewritten back to
            zed-side
  - corr-1  session/cancel notification: forwarded with sid rewrite
  - §7      clean shutdown on stdin EOF: exit 0, no traceback

Multi-session and tool_call/permission flows are exercised in real Zed
(see commit message); not covered here because they require the real
claude-acp child or a much fatter mock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CHILD = Path(__file__).resolve().parent / "fixtures" / "fake_acp_child.py"


class ACPSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "playmaker.acp",
                "--child",
                f"{sys.executable} {FAKE_CHILD}",
            ],
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
        # Close pipe handles to silence ResourceWarning under unittest.
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

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
        raise TimeoutError("no response from proxy within timeout")

    def test_full_session_lifecycle(self) -> None:
        # Initialize handshake — corr-6.
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )
        init = self._recv()
        self.assertEqual(init["id"], 0)
        self.assertEqual(init["result"]["agentInfo"]["name"], "playmaker")
        self.assertEqual(init["result"]["agentInfo"]["title"], "Playmaker")
        # Phase 2: static caps. We declare loadSession + close, no _meta passthrough.
        self.assertTrue(init["result"]["agentCapabilities"]["loadSession"])
        self.assertIn("close", init["result"]["agentCapabilities"]["sessionCapabilities"])

        # session/new — proxy must mint its own sid (§3).
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": str(REPO_ROOT), "mcpServers": []},
            }
        )
        new = self._recv()
        zed_sid = new["result"]["sessionId"]
        self.assertEqual(new["id"], 1)
        self.assertNotEqual(
            zed_sid, "CHILD-SID-1", "proxy must mint its own sid, not echo child's"
        )

        # session/prompt — verify chunk update has rewritten sid (§4).
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": zed_sid,
                    "prompt": [{"type": "text", "text": "hi"}],
                },
            }
        )
        upd = self._recv()
        self.assertEqual(upd["method"], "session/update")
        self.assertEqual(upd["params"]["sessionId"], zed_sid)

        prompt_resp = self._recv()
        self.assertEqual(prompt_resp["id"], 2)
        self.assertEqual(prompt_resp["result"]["stopReason"], "end_turn")

        # session/cancel notification — corr-1: forwarded, no pending touch.
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": zed_sid},
            }
        )
        # No reply is expected (notification). Give the fake child time to log.
        time.sleep(0.2)

        # Clean shutdown on stdin EOF — §7.
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        rc = self.proc.wait(timeout=5)
        stderr = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
        self.assertEqual(rc, 0, f"proxy did not exit cleanly; stderr:\n{stderr}")
        self.assertNotIn(
            "readuntil() called while another coroutine",
            stderr,
            "stdout-drain race regressed (see fix in proxy._start_drain_once)",
        )
        # Confirm fake child saw our cancel with the rewritten sid.
        self.assertIn("received cancel for CHILD-SID-1", stderr)


if __name__ == "__main__":
    unittest.main()

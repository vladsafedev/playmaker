"""Codex CLI handler.

Empirically (codex 0.125.0):
- oneshot: `codex exec [--json] [--skip-git-repo-check] [--cd <DIR>] "<PROMPT>"`
- json output: `--json` (JSONL event stream on stdout)
- last assistant text: `--output-last-message <FILE>` writes it cleanly
- session file: ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO-ts>-<thread_id>.jsonl

stdout event types observed: thread.started, turn.started, item.completed, turn.completed
session-file event types: session_meta, turn_context, event_msg, response_item
The final assistant message is in `task_complete.payload.last_agent_message`
inside the rollout file, OR via stdout `item.completed` events with
`item.type == "agent_message"`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn


CODEX_SESSIONS_ROOT = Path("~/.codex/sessions").expanduser()


class CodexHandler:
    name = "codex"

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
    ) -> DispatchResult:
        full_prompt = self._build_prompt(prompt, files or [])

        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, prefix="codex-last-"
        ) as tmp:
            last_msg_path = Path(tmp.name)

        cmd = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            str(cwd),
            "-o",
            str(last_msg_path),
            full_prompt,
        ]
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        thread_id, last_message_streamed, stdout_buf = self._consume_stream(
            proc, on_session_started
        )
        stderr_buf = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        duration = time.monotonic() - t0

        # Codex emits a non-fatal "failed to record rollout items" warning to stderr
        # when the agent shuts down — exit code is still 0 and the file is written.
        if proc.returncode != 0 and not last_msg_path.exists():
            raise RuntimeError(
                f"codex failed (exit {proc.returncode}): "
                f"{stderr_buf.strip() or stdout_buf[:500]}"
            )

        if thread_id is None:
            raise RuntimeError(
                f"codex stdout missing thread.started event:\n{stdout_buf[:500]}"
            )

        last_message = ""
        if last_msg_path.exists():
            last_message = last_msg_path.read_text(encoding="utf-8").strip()
            last_msg_path.unlink(missing_ok=True)
        if not last_message:
            last_message = last_message_streamed

        return DispatchResult(
            agent_session_id=thread_id,
            cwd=str(cwd),
            session_file=self.find_session_file(thread_id, cwd),
            initial_output=last_message,
            cost_usd=None,  # Codex JSON does not expose USD cost
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    @staticmethod
    def _consume_stream(
        proc: subprocess.Popen,
        on_session_started: SessionStartedCallback | None,
    ) -> tuple[str | None, str, str]:
        """Read stdout line-by-line, fire callback on thread.started, capture
        the latest agent_message item. Returns (thread_id, last_message, raw_stdout).
        """
        thread_id: str | None = None
        last_message = ""
        buf_parts: list[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            buf_parts.append(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = obj.get("type")
            if thread_id is None and etype == "thread.started":
                tid = obj.get("thread_id")
                if isinstance(tid, str) and tid:
                    thread_id = tid
                    if on_session_started is not None:
                        try:
                            on_session_started(tid)
                        except Exception:
                            # callback failures must not break dispatch
                            pass
            elif etype == "item.completed":
                item = obj.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    last_message = item["text"]
        return thread_id, last_message, "".join(buf_parts)

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
    ) -> DispatchResult:
        full_prompt = self._build_prompt(prompt, files or [])

        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, prefix="codex-last-"
        ) as tmp:
            last_msg_path = Path(tmp.name)

        # `codex exec resume` has no --cd; cwd flows through subprocess.
        cmd = [
            "codex",
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "-o",
            str(last_msg_path),
            agent_session_id,
            full_prompt,
        ]
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        thread_id, last_message_streamed, stdout_buf = self._consume_stream(
            proc, on_session_started
        )
        stderr_buf = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        duration = time.monotonic() - t0

        if proc.returncode != 0 and not last_msg_path.exists():
            raise RuntimeError(
                f"codex resume failed (exit {proc.returncode}): "
                f"{stderr_buf.strip() or stdout_buf[:500]}"
            )

        # On resume, codex re-emits thread.started with the SAME thread_id.
        # If the event was missed (older codex builds, transport hiccup), fall
        # back to the input id and fire the callback so the contract holds.
        effective_id = thread_id or agent_session_id
        if thread_id is None and on_session_started is not None:
            try:
                on_session_started(effective_id)
            except Exception:
                pass

        last_message = ""
        if last_msg_path.exists():
            last_message = last_msg_path.read_text(encoding="utf-8").strip()
            last_msg_path.unlink(missing_ok=True)
        if not last_message:
            last_message = last_message_streamed

        return DispatchResult(
            agent_session_id=effective_id,
            cwd=str(cwd),
            session_file=self.find_session_file(effective_id, cwd),
            initial_output=last_message,
            cost_usd=None,
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        # filename: rollout-<ISO timestamp>-<thread_id>.jsonl
        matches = sorted(
            CODEX_SESSIONS_ROOT.rglob(f"rollout-*-{agent_session_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Read Codex rollout jsonl and yield user/assistant/tool turns.

        Codex line shape: {timestamp, type, payload}. Relevant types:
        - response_item with payload.type == "message"  → user/assistant text
        - response_item with payload.type == "function_call" → tool_call
        - response_item with payload.type == "function_call_output" → tool_result
        - event_msg with payload.type == "task_complete" → carries the
          last_agent_message (already produced by some 'message' item)

        Reasoning items are ignored by default — they're verbose and the
        coach reads them only when actively debugging.
        """
        from datetime import datetime

        turns: list[Turn] = []
        if not path.exists():
            return turns
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload") or {}
                ptype = payload.get("type")

                ts = None
                ts_raw = obj.get("timestamp")
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                if ptype == "message":
                    role = payload.get("role") or "user"
                    if role == "developer":
                        # System scaffolding from Codex (skills/permissions); skip.
                        continue
                    content_blocks = payload.get("content") or []
                    texts: list[str] = []
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        text = block.get("text") or ""
                        if text:
                            texts.append(text)
                    turns.append(
                        Turn(
                            role=role,
                            content="\n".join(texts),
                            timestamp=ts,
                        )
                    )
                elif ptype == "function_call":
                    turns.append(
                        Turn(
                            role="assistant",
                            content="",
                            tool_calls=[
                                {
                                    "id": payload.get("call_id") or payload.get("id"),
                                    "name": payload.get("name"),
                                    "input": payload.get("arguments"),
                                }
                            ],
                            timestamp=ts,
                        )
                    )
                elif ptype == "function_call_output":
                    output = payload.get("output")
                    turns.append(
                        Turn(
                            role="tool",
                            content="",
                            tool_results=[
                                {
                                    "tool_use_id": payload.get("call_id"),
                                    "content": output
                                    if isinstance(output, str)
                                    else json.dumps(output) if output else "",
                                }
                            ],
                            timestamp=ts,
                        )
                    )
        return turns

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = "\n".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

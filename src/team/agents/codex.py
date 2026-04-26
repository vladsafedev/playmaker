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

from team.agents.base import DispatchResult, Turn


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
        proc = subprocess.run(cmd, capture_output=True, text=True)
        duration = time.monotonic() - t0

        # Codex emits a non-fatal "failed to record rollout items" warning to stderr
        # when the agent shuts down — exit code is still 0 and the file is written.
        if proc.returncode != 0 and not last_msg_path.exists():
            raise RuntimeError(
                f"codex failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )

        thread_id = self._parse_thread_id(proc.stdout)
        if thread_id is None:
            raise RuntimeError(
                f"codex stdout missing thread.started event:\n{proc.stdout[:500]}"
            )

        last_message = ""
        if last_msg_path.exists():
            last_message = last_msg_path.read_text(encoding="utf-8").strip()
            last_msg_path.unlink(missing_ok=True)
        # Fallback: scrape stdout if --output-last-message didn't produce.
        if not last_message:
            last_message = self._parse_last_agent_message(proc.stdout)

        return DispatchResult(
            agent_session_id=thread_id,
            cwd=str(cwd),
            session_file=self.find_session_file(thread_id, cwd),
            initial_output=last_message,
            cost_usd=None,  # Codex JSON does not expose USD cost
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
    def _parse_thread_id(stdout: str) -> str | None:
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "thread.started" and "thread_id" in obj:
                return obj["thread_id"]
        return None

    @staticmethod
    def _parse_last_agent_message(stdout: str) -> str:
        last = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "item.completed":
                item = obj.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    last = item["text"]
        return last

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = "\n".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

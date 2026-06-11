"""Gemini CLI handler.

Empirically:
- oneshot: `gemini -p "<prompt>" -o json [--yolo] [--include-directories <list>]`
- session file: ~/.gemini/tmp/<cwd-basename>/chats/session-<timestamp>-<short-id>.<ext>
  where short-id is first 8 chars of session_id
- TWO file formats coexist in chats/:
  - .jsonl  (interactive sessions: line-per-event, types metadata|user|gemini)
  - .json   (single object {sessionId, projectHash, startTime, kind, messages: [...]})
    written by non-interactive `-p` runs
- stdout json output: {session_id, response, stats: {models: {...}}}
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn


GEMINI_CHATS_ROOT = Path("~/.gemini/tmp").expanduser()


class GeminiHandler:
    name = "gemini"

    def is_available(self) -> bool:
        return shutil.which("gemini") is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        """Streaming dispatch via `gemini -p ... -o stream-json` so that
        `on_session_started` fires within the first event instead of at
        the very end. The first JSONL line carries `session_id` (or
        `sessionId`); we callback on it so dispatch's state.db write
        happens early — other commands can locate the session almost
        immediately after CLI invocation.
        """
        full_prompt = self._build_prompt(prompt, files or [])
        cmd = [
            "gemini",
            "-p",
            full_prompt,
            "-o",
            "stream-json",
            "--yolo",
        ]
        if model:
            cmd += ["-m", model]
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        agent_session_id: str | None = None
        last_response = ""
        first_lines: list[str] = []

        assert proc.stdout is not None
        for raw in proc.stdout:
            if len(first_lines) < 3:
                first_lines.append(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if agent_session_id is None:
                sid = obj.get("session_id") or obj.get("sessionId")
                if sid:
                    agent_session_id = sid
                    if on_session_started is not None:
                        try:
                            on_session_started(agent_session_id)
                        except Exception:
                            pass

            # Capture latest "response" field (gemini emits incremental
            # updates that overwrite each other; the last value wins).
            if obj.get("response"):
                last_response = obj["response"]

        stderr_buf = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        duration = time.monotonic() - t0

        if proc.returncode != 0:
            raise RuntimeError(
                f"gemini failed (exit {proc.returncode}): "
                f"{stderr_buf.strip() or ''.join(first_lines)[:500]}"
            )

        if not agent_session_id:
            raise RuntimeError(
                f"gemini stream-json missing session_id; first lines:\n"
                f"{''.join(first_lines)[:500]}"
            )

        # `gemini -p -o stream-json` does not reliably emit a `response` field
        # in the stream (varies by version), so `last_response` can be empty
        # even on success. The final answer is always in the session file —
        # fall back to its last assistant turn so callers get real output.
        session_file = self.find_session_file(agent_session_id, cwd)
        initial_output = last_response or self._last_assistant_text(session_file)
        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=session_file,
            initial_output=initial_output,
            cost_usd=None,  # Gemini json reports tokens, not USD
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        # Gemini's --resume takes an INDEX (or "latest"), not a UUID — resolve
        # it from `gemini --list-sessions` run in the same cwd, since the list
        # is per-project.
        index = self._resolve_session_index(agent_session_id, cwd)
        if index is None:
            raise RuntimeError(
                f"gemini --list-sessions has no entry matching {agent_session_id!r} in {cwd}"
            )

        full_prompt = self._build_prompt(prompt, files or [])
        cmd = [
            "gemini",
            "--resume",
            str(index),
            "-p",
            full_prompt,
            "-o",
            "json",
            "--yolo",
        ]
        if model:
            cmd += ["-m", model]
        t0 = time.monotonic()
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
        duration = time.monotonic() - t0

        if proc.returncode != 0:
            raise RuntimeError(
                f"gemini resume failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"gemini resume returned non-JSON output: {proc.stdout[:500]}"
            ) from e

        # Gemini keeps the same session_id on resume.
        new_session_id = data.get("session_id") or data.get("sessionId") or agent_session_id
        if on_session_started is not None:
            try:
                on_session_started(new_session_id)
            except Exception:
                pass
        session_file = self.find_session_file(new_session_id, cwd)
        initial_output = data.get("response", "") or self._last_assistant_text(session_file)
        return DispatchResult(
            agent_session_id=new_session_id,
            cwd=str(cwd),
            session_file=session_file,
            initial_output=initial_output,
            cost_usd=None,
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    def _last_assistant_text(self, session_file: Path | None) -> str:
        """Last non-empty assistant turn from the session file.

        Used as a fallback when the CLI's streamed/`-o json` `response` field
        is empty. The file can lag process exit slightly, so retry briefly.
        """
        if session_file is None:
            return ""
        for _ in range(5):
            for turn in reversed(self.parse_session_file(session_file)):
                if turn.role == "assistant" and turn.content.strip():
                    return turn.content
            time.sleep(0.2)
        return ""

    @staticmethod
    def _resolve_session_index(uuid: str, cwd: Path) -> int | None:
        """Run `gemini --list-sessions` in cwd and find the index whose UUID matches.

        Output format (one session per line):
            "  <N>. <title> (<age>) [<uuid>]"
        """
        proc = subprocess.run(
            ["gemini", "--list-sessions"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        # Capture leading index AND trailing bracketed uuid; lazy middle.
        pattern = re.compile(r"^\s*(\d+)\..*\[([0-9a-f-]+)\]\s*$")
        for line in proc.stdout.splitlines():
            m = pattern.match(line)
            if m and m.group(2) == uuid:
                return int(m.group(1))
        return None

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        chats_dir = GEMINI_CHATS_ROOT / cwd.name / "chats"
        if not chats_dir.is_dir():
            return None
        short_id = agent_session_id[:8]
        matches: list[Path] = []
        for ext in ("json", "jsonl"):
            matches.extend(chats_dir.glob(f"session-*-{short_id}.{ext}"))
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return None
        # Prefer the one whose first JSON value carries this exact sessionId.
        for path in matches:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    if path.suffix == ".jsonl":
                        head = fh.readline().strip()
                        if not head:
                            continue
                        obj = json.loads(head)
                    else:
                        obj = json.load(fh)
                if obj.get("sessionId") == agent_session_id:
                    return path
            except (OSError, json.JSONDecodeError):
                continue
        return matches[0]

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Read Gemini session file and yield turns.

        Two file formats coexist:
        - .json   single object {sessionId, ..., messages: [...]}
        - .jsonl  one event per line: {kind: 'main', ...} metadata
                  and {id, timestamp, type: user|gemini, content}

        For both, message content is either a list[{text}] (user) or
        a string (gemini), or sometimes empty (token-only chunk).
        """

        turns: list[Turn] = []
        if not path.exists():
            return turns

        if path.suffix == ".json":
            try:
                with path.open("r", encoding="utf-8") as fh:
                    obj = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return turns
            for msg in obj.get("messages", []):
                turn = self._gemini_msg_to_turn(msg)
                if turn is not None:
                    turns.append(turn)
            return turns

        # .jsonl
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("kind") == "main" and "type" not in obj:
                    continue  # session metadata
                turn = self._gemini_msg_to_turn(obj)
                if turn is not None:
                    turns.append(turn)
        return turns

    @staticmethod
    def _gemini_msg_to_turn(msg: dict) -> Turn | None:
        from datetime import datetime

        mtype = msg.get("type")
        if mtype not in ("user", "gemini", "model"):
            return None

        ts = None
        ts_raw = msg.get("timestamp")
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                pass

        content_raw = msg.get("content")
        text = ""
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            chunks = []
            for block in content_raw:
                if isinstance(block, dict):
                    chunks.append(block.get("text") or "")
                elif isinstance(block, str):
                    chunks.append(block)
            text = "\n".join(c for c in chunks if c)

        if not text and not msg.get("thoughts"):
            # Streaming placeholder; ignore.
            return None

        role = "user" if mtype == "user" else "assistant"
        return Turn(role=role, content=text, timestamp=ts)

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = "\n".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

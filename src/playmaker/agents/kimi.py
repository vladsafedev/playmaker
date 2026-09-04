"""Kimi Code CLI handler.

Empirically (kimi-code 0.41.0):
- oneshot: `kimi -m <model> -p "<prompt>" --output-format stream-json`; resume
  uses `kimi -S <session_id> -p ...` in the same working directory
- prompt mode auto-approves tool calls already; it refuses `--auto`, `--yolo`,
  and `--plan`, and it has no read-only flag
- stream-json is JSONL. The final assistant text is in `role: assistant`
  `content`; the session id arrives only in a trailing
  `meta/session.resume_hint` event
- exit 1 is non-retryable (auth, quota, or an unknown model); exit 75 is
  retryable (rate limit or 5xx). This handler does not retry; callers may
  re-dispatch after an exit-75 failure
- sessions live under `$KIMI_CODE_HOME` (or `~/.kimi-code`):
  `sessions/wd_*/session_<uuid>/agents/main/wire.jsonl`, with `state.json`
  alongside the `agents/` directory
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import agent_binary, agent_setting


def kimi_code_home() -> Path:
    """Kimi's session root, respecting its documented environment override."""
    configured = os.environ.get("KIMI_CODE_HOME")
    return Path(configured).expanduser() if configured else Path("~/.kimi-code").expanduser()


class KimiHandler:
    name = "kimi"

    def is_available(self) -> bool:
        return shutil.which(agent_binary("kimi")) is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        return self._run(
            prompt,
            cwd,
            files or [],
            on_session_started=on_session_started,
            model=model,
            session_id=None,
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
        return self._run(
            prompt,
            cwd,
            files or [],
            on_session_started=on_session_started,
            model=model,
            session_id=agent_session_id,
        )

    def _run(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path],
        *,
        on_session_started: SessionStartedCallback | None,
        model: str | None,
        session_id: str | None,
    ) -> DispatchResult:
        effective_model = model or agent_setting("kimi", "model")
        cmd = [agent_binary("kimi")]
        if session_id:
            cmd += ["-S", session_id]
        if effective_model:
            cmd += ["-m", str(effective_model)]
        cmd += ["-p", self._build_prompt(prompt, files), "--output-format", "stream-json"]

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
        assistant_contents: list[str] = []
        first_lines: list[str] = []
        stderr_parts: list[str] = []
        stderr = proc.stderr

        def drain_stderr() -> None:
            if stderr is not None:
                stderr_parts.append(stderr.read())

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        def remember_session_id(candidate: object) -> None:
            nonlocal agent_session_id
            if agent_session_id is not None or not isinstance(candidate, str) or not candidate:
                return
            agent_session_id = candidate
            if on_session_started is not None:
                try:
                    on_session_started(candidate)
                except Exception:
                    pass

        assert proc.stdout is not None
        for raw in proc.stdout:
            if len(first_lines) < 3:
                first_lines.append(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            if event.get("role") == "assistant":
                content = event.get("content")
                if isinstance(content, str) and content:
                    assistant_contents.append(content)
            elif event.get("role") == "meta" and event.get("type") == "session.resume_hint":
                remember_session_id(event.get("session_id"))

        proc.wait()
        stderr_thread.join()
        stderr_text = "".join(stderr_parts)
        duration = time.monotonic() - t0

        if proc.returncode != 0:
            detail = stderr_text.strip() or "".join(first_lines)[:500] or "(no error output)"
            raise RuntimeError(f"kimi failed (exit {proc.returncode}): {detail}")

        if agent_session_id is None:
            remember_session_id(self._latest_session_id(cwd))
        if agent_session_id is None:
            raise RuntimeError(
                "kimi stream-json missing session.resume_hint; first lines:\n"
                f"{''.join(first_lines)[:500]}"
            )

        session_file = self.find_session_file(agent_session_id, cwd)
        initial_output = next(
            (content for content in reversed(assistant_contents) if content.strip()), ""
        ) or self._last_assistant_text(session_file)
        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=session_file,
            initial_output=initial_output,
            cost_usd=None,
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    def _latest_session_id(self, cwd: Path) -> str | None:
        """Ask Kimi's per-working-directory index for its most recent session."""
        try:
            proc = subprocess.run(
                [agent_binary("kimi"), "session", "list", "--json"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None

        entries = self._session_list_entries(proc.stdout)
        # `kimi session list --help` says entries are most recently updated
        # first. Sort timestamped listings too, so an unordered response does
        # not bind a dispatch to an older session.
        entries.sort(key=self._session_entry_timestamp, reverse=True)
        expected_cwd = self._resolved_cwd(cwd)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            listed_cwd = entry.get("cwd") or entry.get("workDir")
            if isinstance(listed_cwd, str) and self._resolved_cwd(Path(listed_cwd)) != expected_cwd:
                continue
            for key in ("id", "session_id", "sessionId"):
                candidate = entry.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
        return None

    @staticmethod
    def _session_entry_timestamp(entry: object) -> tuple[int, float]:
        if not isinstance(entry, dict):
            return (0, 0)
        for key in ("updatedAt", "lastUsedAt", "updated_at", "last_used_at"):
            raw = entry.get(key)
            if isinstance(raw, (int, float)):
                return (1, float(raw))
            if isinstance(raw, str):
                try:
                    return (1, float(raw))
                except ValueError:
                    try:
                        return (1, datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
                    except (OSError, OverflowError, ValueError):
                        continue
        return (0, 0)

    @staticmethod
    def _session_list_entries(raw: str) -> list[object]:
        """Accept the array and object envelopes used by Kimi session listings."""
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return decoded
        if isinstance(decoded, dict):
            for key in ("sessions", "items", "data"):
                value = decoded.get(key)
                if isinstance(value, list):
                    return value
            return [decoded]

        entries: list[object] = []
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        sessions_root = kimi_code_home() / "sessions"
        if not sessions_root.is_dir():
            return None
        matches = list(sessions_root.glob(f"wd_*/{agent_session_id}/agents/main/wire.jsonl"))
        matches.sort(key=self._mtime, reverse=True)
        expected_cwd = self._resolved_cwd(cwd)
        unverified_matches: list[Path] = []
        saw_state_file = False
        for path in matches:
            state_path = path.parents[2] / "state.json"
            if not state_path.exists():
                unverified_matches.append(path)
                continue
            saw_state_file = True
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict) or state.get("id") != agent_session_id:
                continue
            state_cwd = state.get("cwd")
            if isinstance(state_cwd, str) and self._resolved_cwd(Path(state_cwd)) == expected_cwd:
                return path
        if saw_state_file:
            return None
        workspace_prefix = f"wd_{expected_cwd.name}_"
        for path in unverified_matches:
            if path.parents[3].name.startswith(workspace_prefix):
                return path
        return None

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Normalize Kimi's append-only `wire.jsonl` without runtime noise."""
        turns: list[Turn] = []
        assistants: dict[str, dict[str, Any]] = {}
        call_turns: dict[str, str] = {}
        if not path.exists():
            return turns

        def flush(turn_id: str) -> None:
            assistant = assistants.pop(turn_id, None)
            if assistant is None:
                return
            content = "\n".join(assistant["content"])
            if content or assistant["tool_calls"] or assistant["tool_results"]:
                turns.append(
                    Turn(
                        role="assistant",
                        content=content,
                        tool_calls=assistant["tool_calls"],
                        tool_results=assistant["tool_results"],
                        timestamp=assistant["timestamp"],
                    )
                )

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return turns
        for raw in lines:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "turn.prompt":
                for turn_id in list(assistants):
                    flush(turn_id)
                content = self._text_blocks(event.get("input"))
                turns.append(Turn(role="user", content=content, timestamp=self._timestamp(event)))
                continue
            if event_type == "turn.ended":
                turn_id = str(event.get("turnId", ""))
                if turn_id:
                    flush(turn_id)
                continue
            if event_type != "context.append_loop_event":
                continue

            loop_event = event.get("event")
            if not isinstance(loop_event, dict):
                continue
            loop_type = loop_event.get("type")
            tool_id = loop_event.get("toolCallId")
            turn_id = str(loop_event.get("turnId", ""))
            if loop_type == "tool.result" and not turn_id and isinstance(tool_id, str):
                turn_id = call_turns.get(tool_id, "")
            if not turn_id:
                continue
            assistant = assistants.setdefault(
                turn_id,
                {
                    "content": [],
                    "tool_calls": [],
                    "tool_results": [],
                    "timestamp": self._timestamp(event),
                },
            )
            if loop_type == "content.part":
                part = loop_event.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        assistant["content"].append(text)
            elif loop_type == "tool.call":
                name = loop_event.get("name")
                if isinstance(tool_id, str) and isinstance(name, str):
                    call_turns[tool_id] = turn_id
                    assistant["tool_calls"].append(
                        {
                            "id": tool_id,
                            "name": name,
                            "input": self._tool_input(loop_event.get("args")),
                        }
                    )
            elif loop_type == "tool.result":
                if isinstance(tool_id, str):
                    assistant["tool_results"].append(
                        {
                            "tool_use_id": tool_id,
                            "content": self._tool_result(loop_event.get("result")),
                        }
                    )

        for turn_id in list(assistants):
            flush(turn_id)
        return turns

    def _last_assistant_text(self, session_file: Path | None) -> str:
        if session_file is None:
            return ""
        for turn in reversed(self.parse_session_file(session_file)):
            if turn.role == "assistant" and turn.content.strip():
                return turn.content
        return ""

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = "\n".join(f"@{path}" for path in files)
        return f"{prompt}\n\n{refs}"

    @staticmethod
    def _resolved_cwd(cwd: Path) -> Path:
        return cwd.expanduser().resolve()

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    @staticmethod
    def _timestamp(event: dict[str, Any]) -> datetime | None:
        raw = event.get("time")
        if not isinstance(raw, (int, float)):
            return None
        return datetime.fromtimestamp(raw / 1000, tz=UTC)

    @staticmethod
    def _text_blocks(raw: object) -> str:
        if isinstance(raw, str):
            return raw
        if not isinstance(raw, list):
            return ""
        texts: list[str] = []
        for block in raw:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        return "\n".join(texts)

    @staticmethod
    def _tool_input(raw: object) -> object:
        if not isinstance(raw, str):
            return raw
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    @staticmethod
    def _tool_result(raw: object) -> str:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for key in ("output", "content"):
                value = raw.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(raw, ensure_ascii=False)
        return "" if raw is None else str(raw)

"""Antigravity CLI (`agy`) handler.

Empirically (agy 1.1.1):
- oneshot: `agy -p "<prompt>" [--model "<display name>"] [--dangerously-skip-permissions]
  [--print-timeout 60m] [--log-file <path>]`; stdout is the final response as
  plain text (no JSON envelope).
- resume: `agy --conversation <uuid> -p "..."` — keeps the same conversation id.
- models are addressed by *display name* from `agy models`, e.g.
  "Claude Opus 4.6 (Thinking)", "Gemini 3.5 Flash (Low)".
- the conversation id is NOT printed to stdout; it is recovered from the CLI
  debug log (`--log-file`) via the `Created conversation <uuid>` line, which
  appears a few seconds in — that is our early on_session_started signal.
- session transcript is plain JSONL, one step per line:
  ~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl
  with fields: step_index, source (USER_EXPLICIT|SYSTEM|MODEL), type
  (USER_INPUT|PLANNER_RESPONSE|RUN_COMMAND|CODE_ACTION|VIEW_FILE|...),
  status, created_at, content.
- the agent's shell cwd is agy's private scratch dir
  (~/.gemini/antigravity-cli/scratch), NOT the workspace — relative file writes
  land there. dispatch() therefore prepends a workspace preamble instructing
  the agent to use absolute paths under the target cwd.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import agent_setting, yolo_enabled

AGY_BRAIN_ROOT = Path("~/.gemini/antigravity-cli/brain").expanduser()

# `Created conversation <uuid>` for fresh runs; resumes keep the known id.
_CONVERSATION_RE = re.compile(r"Created conversation ([0-9a-f-]{36})")

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.DOTALL)


class AgyHandler:
    name = "agy"

    def is_available(self) -> bool:
        return shutil.which("agy") is not None

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def available_models() -> tuple[str, ...]:
        """Exact model display names agy accepts, from `agy models`.

        agy resolves an unknown `--model` to its default *silently* (no error,
        wrong model runs), so we validate against this list before dispatch.
        Cached per-process; returns () if the roster can't be read (then we skip
        validation rather than block a dispatch on a probe failure).
        """
        if shutil.which("agy") is None:
            return ()
        try:
            proc = subprocess.run(
                ["agy", "models"], capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if proc.returncode != 0:
            return ()
        return tuple(
            line.strip() for line in proc.stdout.splitlines() if line.strip()
        )

    def _validate_model(self, model: str | None) -> None:
        if not model:
            return
        roster = self.available_models()
        if roster and model not in roster:
            raise RuntimeError(
                f"agy has no model {model!r}; agy would silently fall back to its "
                f"default. Available: {', '.join(roster)}"
            )

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
            conversation_id=None,
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
            conversation_id=agent_session_id,
        )

    def _run(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path],
        *,
        on_session_started: SessionStartedCallback | None,
        model: str | None,
        conversation_id: str | None,
    ) -> DispatchResult:
        """Shared oneshot/resume runner.

        stdout/stderr go to temp files (agy prints the whole response at the
        end, and a PIPE nobody drains can deadlock on long answers); while the
        process runs we poll the debug log for the conversation id so
        on_session_started fires within seconds, not at completion.
        """
        self._validate_model(model)
        full_prompt = self._build_prompt(prompt, files, cwd)

        fd_log, log_name = tempfile.mkstemp(prefix="playmaker-agy-", suffix=".log")
        os.close(fd_log)
        log_path = Path(log_name)

        cmd = ["agy", "-p", full_prompt, "--log-file", str(log_path)]
        if conversation_id:
            cmd += ["--conversation", conversation_id]
        # agy has no middle tier: unlike claude there is no per-mode permission
        # flag, so a detached run either skips the prompts or answers nothing.
        # `--sandbox` is orthogonal and can be layered on top.
        if yolo_enabled("agy", default=True):
            cmd.append("--dangerously-skip-permissions")
        if agent_setting("agy", "sandbox", False):
            cmd.append("--sandbox")
        # agy's built-in print timeout is 5m — too short for real subtasks.
        cmd += ["--print-timeout", str(agent_setting("agy", "print_timeout", "60m"))]
        if model:
            cmd += ["--model", model]

        t0 = time.monotonic()
        agent_session_id: str | None = conversation_id
        if agent_session_id and on_session_started is not None:
            try:
                on_session_started(agent_session_id)
            except Exception:
                pass

        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out_fh, \
                tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=out_fh,
                stderr=err_fh,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            try:
                while proc.poll() is None:
                    if agent_session_id is None:
                        agent_session_id = self._conversation_from_log(log_path)
                        if agent_session_id and on_session_started is not None:
                            try:
                                on_session_started(agent_session_id)
                            except Exception:
                                pass
                    time.sleep(0.3)
            except BaseException:
                proc.kill()
                raise
            duration = time.monotonic() - t0

            out_fh.seek(0)
            stdout_text = out_fh.read()
            err_fh.seek(0)
            stderr_text = err_fh.read()

        if agent_session_id is None:
            agent_session_id = self._conversation_from_log(log_path)
            if agent_session_id and on_session_started is not None:
                try:
                    on_session_started(agent_session_id)
                except Exception:
                    pass

        log_tail = self._log_tail(log_path)
        log_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            raise RuntimeError(
                f"agy failed (exit {proc.returncode}): "
                f"{stderr_text.strip() or stdout_text.strip()[:500] or log_tail}"
            )
        if not agent_session_id:
            raise RuntimeError(
                f"agy finished but no conversation id found in its log; tail:\n{log_tail}"
            )

        session_file = self.find_session_file(agent_session_id, cwd)
        initial_output = stdout_text.strip() or self._last_assistant_text(session_file)
        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=session_file,
            initial_output=initial_output,
            cost_usd=None,  # agy reports neither tokens nor USD in print mode
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    @staticmethod
    def _conversation_from_log(log_path: Path) -> str | None:
        try:
            m = _CONVERSATION_RE.search(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None
        return m.group(1) if m else None

    @staticmethod
    def _log_tail(log_path: Path, lines: int = 15) -> str:
        try:
            return "\n".join(
                log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            )
        except OSError:
            return ""

    def _last_assistant_text(self, session_file: Path | None) -> str:
        """Fallback when stdout is empty: last non-empty assistant turn.

        The transcript can lag process exit slightly, so retry briefly.
        """
        if session_file is None:
            return ""
        for _ in range(5):
            for turn in reversed(self.parse_session_file(session_file)):
                if turn.role == "assistant" and turn.content.strip():
                    return turn.content
            time.sleep(0.2)
        return ""

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        # Transcripts are keyed by conversation id only — cwd is irrelevant.
        logs_dir = AGY_BRAIN_ROOT / agent_session_id / ".system_generated" / "logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            path = logs_dir / name
            if path.exists():
                return path
        return None

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Read the brain transcript JSONL and yield normalized turns.

        - USER_INPUT       -> user (unwrapped from <USER_REQUEST> tags)
        - PLANNER_RESPONSE -> assistant (empty ones are streaming placeholders)
        - other MODEL steps (RUN_COMMAND, CODE_ACTION, VIEW_FILE, ...) -> tool
        - SYSTEM steps (CONVERSATION_HISTORY, CHECKPOINT) -> skipped
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

                source = obj.get("source")
                step_type = obj.get("type")
                content = obj.get("content") or ""

                ts = None
                ts_raw = obj.get("created_at")
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                if step_type == "USER_INPUT":
                    m = _USER_REQUEST_RE.search(content)
                    turns.append(
                        Turn(
                            role="user",
                            content=(m.group(1) if m else content).strip(),
                            timestamp=ts,
                        )
                    )
                elif step_type == "PLANNER_RESPONSE":
                    if content.strip():
                        turns.append(Turn(role="assistant", content=content, timestamp=ts))
                elif source == "MODEL":
                    turns.append(
                        Turn(
                            role="tool",
                            content="",
                            tool_calls=[{"id": None, "name": step_type, "input": None}],
                            tool_results=[{"tool_use_id": None, "content": content}],
                            timestamp=ts,
                        )
                    )
        return turns

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path], cwd: Path) -> str:
        # The agy agent's shell starts in a private scratch dir; without this
        # preamble, "create foo.txt" lands in ~/.gemini/antigravity-cli/scratch
        # instead of the workspace (verified empirically on agy 1.1.1).
        parts = [
            f"Workspace root: {cwd}\n"
            "Do all file reads and writes inside the workspace root, always using "
            "absolute paths — your shell's default cwd is a private scratch "
            "directory outside the workspace, so relative paths would land there.",
            prompt,
        ]
        if files:
            parts.append("Relevant files:\n" + "\n".join(str(p) for p in files))
        return "\n\n".join(parts)

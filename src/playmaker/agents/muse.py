"""Muse Code CLI handler.

Empirically (muse 1.4.1): `muse exec --json [options] -- <prompt>` writes
JSONL to stdout.  The first session-stream envelope contains the session id;
`run.terminal.*` holds the final answer and terminal state.  Native sessions
live below `$XDG_DATA_HOME/muse` (or `~/.local/share/muse`) and their
`recorded_at` values are epoch microseconds.  Headless runs need approval
disabled; `--yolo` is the explicit escape hatch for work that needs to write
outside Muse's workspace sandbox.
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
from playmaker.config import agent_binary, agent_setting, yolo_enabled


def muse_data_home() -> Path:
    """Muse's documented XDG data root."""
    configured = os.environ.get("XDG_DATA_HOME")
    return (
        Path(configured).expanduser() / "muse"
        if configured
        else Path("~/.local/share/muse").expanduser()
    )


def permission_args() -> list[str]:
    """Build Muse's headless permission policy from ``[agents.muse]``.

    Approval must be disabled or headless tool calls stall.  The normal sandbox
    prevents writes outside the workspace (for example ``~/.cache/uv``), so
    ``yolo = true`` is the deliberate escape hatch for such work.
    """
    if yolo_enabled("muse", default=False):
        return ["--yolo"]
    args = ["--disable-approval"]
    if agent_setting("muse", "trust_workspace", True) is not False:
        args.append("--trust-workspace")
    for key, flag in (
        ("sandbox_network", "--sandbox-network"),
        ("permission_profile", "--permission-profile"),
    ):
        value = agent_setting("muse", key)
        if value is not None:
            args += [flag, str(value)]
    return args


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def find_session_file(agent_session_id: str, cwd: Path) -> Path | None:
    """Find the newest matching root session whose metadata matches ``cwd``."""
    root = muse_data_home() / "sessions"
    if not root.is_dir():
        return None
    candidates = list(root.glob(f"*/*/*/{agent_session_id}/session.jsonl"))
    candidates.sort(key=_mtime, reverse=True)
    expected_cwd = _resolved(cwd)
    for path in candidates:
        metadata_seen = False
        workspace_matches = False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(record, dict)
                or record.get("payload_type") != "runtime.session.metadata"
            ):
                continue
            metadata_seen = True
            payload = record.get("payload")
            workspace = (
                payload.get("record", {}).get("workspace_root")
                if isinstance(payload, dict)
                else None
            )
            if isinstance(workspace, str) and _resolved(Path(workspace)) == expected_cwd:
                workspace_matches = True
                break
        if workspace_matches or not metadata_seen:
            return path
    return None


def _timestamp(record: dict[str, Any]) -> datetime | None:
    value = record.get("recorded_at")
    return (
        datetime.fromtimestamp(value / 1_000_000, tz=UTC)
        if isinstance(value, (int, float))
        else None
    )


def parse_session_file(path: Path) -> list[Turn]:
    """Normalize Muse's root-session run events, ignoring retained wrappers."""
    turns: list[Turn] = []
    runs: dict[str, dict[str, Any]] = {}

    def flush(run_id: str) -> None:
        run = runs.pop(run_id, None)
        if run is None:
            return
        content = "\n".join(run["content"])
        if content or run["tool_calls"] or run["tool_results"]:
            turns.append(
                Turn(
                    role="assistant",
                    content=content,
                    tool_calls=run["tool_calls"],
                    tool_results=run["tool_results"],
                    timestamp=run["timestamp"],
                )
            )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return turns
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or "retained_frame" in record:
            continue
        if record.get("payload_type") != "runtime.session":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("kind") != "run":
            continue
        run_id = payload.get("run_id")
        event = payload.get("event")
        if not isinstance(run_id, str) or not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind == "started":
            for active_id in list(runs):
                flush(active_id)
            prompt = event.get("prompt")
            turns.append(
                Turn(
                    role="user",
                    content=prompt if isinstance(prompt, str) else "",
                    timestamp=_timestamp(record),
                )
            )
            runs[run_id] = {
                "content": [],
                "tool_calls": [],
                "tool_results": [],
                "timestamp": _timestamp(record),
            }
            continue
        run = runs.setdefault(
            run_id,
            {"content": [], "tool_calls": [], "tool_results": [], "timestamp": _timestamp(record)},
        )
        if kind == "assistant_tool_calls_committed":
            calls = event.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    call_id, name = call.get("call_id"), call.get("name")
                    if isinstance(call_id, str) and isinstance(name, str):
                        args = call.get("args")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                pass
                        run["tool_calls"].append({"id": call_id, "name": name, "input": args})
        elif kind == "tool_result_batch_committed":
            results = event.get("results")
            if isinstance(results, list):
                for result in results:
                    if isinstance(result, dict) and isinstance(result.get("tool_call_id"), str):
                        text = result.get("text")
                        run["tool_results"].append(
                            {
                                "tool_use_id": result["tool_call_id"],
                                "content": text if isinstance(text, str) else "",
                            }
                        )
        elif kind == "assistant_message_committed":
            text = event.get("text")
            if isinstance(text, str) and text:
                run["content"].append(text)
        elif kind == "terminal":
            flush(run_id)
    for run_id in list(runs):
        flush(run_id)
    return turns


class MuseHandler:
    name = "muse"

    def is_available(self) -> bool:
        return shutil.which(agent_binary("muse")) is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        return self._run(prompt, cwd, files or [], on_session_started, model, None)

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        return self._run(prompt, cwd, files or [], on_session_started, model, agent_session_id)

    def _run(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path],
        on_session_started: SessionStartedCallback | None,
        model: str | None,
        session_id: str | None,
    ) -> DispatchResult:
        cmd = [agent_binary("muse"), "exec", "--json"]
        if session_id:
            cmd += ["--session-id", session_id]
        effective_model = model or agent_setting("muse", "model")
        if effective_model:
            cmd += ["--model", str(effective_model)]
        effort = agent_setting("muse", "reasoning_effort")
        if effort:
            cmd += ["--reasoning-effort", str(effort)]
        cmd += permission_args() + ["--", self._build_prompt(prompt, files)]
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
        stderr_parts: list[str] = []
        stderr = proc.stderr
        thread = threading.Thread(
            target=lambda: stderr_parts.append(stderr.read()) if stderr is not None else None,
            daemon=True,
        )
        thread.start()
        agent_session_id: str | None = None
        first_lines: list[str] = []
        terminal_text = ""
        terminal_state: str | None = None
        terminal_reason = ""

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
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            stream = event.get("stream")
            if isinstance(stream, dict) and stream.get("kind") == "session":
                remember_session_id(stream.get("id"))
            if isinstance(event.get("payload_type"), str) and event["payload_type"].startswith(
                "run.terminal."
            ):
                payload = event.get("payload")
                if isinstance(payload, dict):
                    terminal_state = (
                        payload.get("terminal")
                        if isinstance(payload.get("terminal"), str)
                        else None
                    )
                    terminal_text = (
                        payload.get("text") if isinstance(payload.get("text"), str) else ""
                    )
                    terminal_reason = (
                        payload.get("reason") if isinstance(payload.get("reason"), str) else ""
                    )
        proc.wait()
        thread.join()
        duration = time.monotonic() - t0
        stderr_text = self._error_stderr("".join(stderr_parts))
        if proc.returncode != 0 or terminal_state != "completed":
            detail = (
                terminal_reason or stderr_text or "".join(first_lines)[:500] or "(no error output)"
            )
            raise RuntimeError(f"muse failed (exit {proc.returncode}): {detail}")
        if agent_session_id is None:
            raise RuntimeError(
                "muse stdout missing session stream id; first lines:\n" + "".join(first_lines)[:500]
            )
        session_file = self.find_session_file(agent_session_id, cwd)
        output = terminal_text or self._last_assistant_text(session_file)
        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=session_file,
            initial_output=output,
            cost_usd=None,
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    @staticmethod
    def _error_stderr(stderr: str) -> str:
        return "\n".join(
            line
            for line in stderr.splitlines()
            if not line.startswith(
                ("muse: workspace root:", "muse: Agent delegation:", "muse: workspace trust:")
            )
        ).strip()

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        return prompt if not files else f"{prompt}\n\n" + "\n".join(str(path) for path in files)

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        return find_session_file(agent_session_id, cwd)

    def parse_session_file(self, path: Path) -> list[Turn]:
        return parse_session_file(path)

    def _last_assistant_text(self, path: Path | None) -> str:
        if path is None:
            return ""
        for turn in reversed(self.parse_session_file(path)):
            if turn.role == "assistant" and turn.content.strip():
                return turn.content
        return ""

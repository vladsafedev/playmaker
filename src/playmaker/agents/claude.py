"""Claude Code CLI handler.

Empirically:
- oneshot: `claude -p "..."`
- json output: `--output-format json`
- primary cwd: subprocess cwd; `--add-dir` is variadic (multiple dirs) and would
  swallow the positional prompt, so we omit it and rely on subprocess cwd alone
- session file: ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-id>.jsonl
- json result fields: session_id, result, total_cost_usd, duration_ms, usage
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import (
    agent_binary,
    agent_list_setting,
    agent_setting,
    yolo_enabled,
)

# What a sub-agent is allowed to do without a human at the keyboard.
# Verified against claude 2.x in `-p` mode:
#   default       -> the agent writes nothing and answers "I need permission";
#                    it does NOT hang, it just returns having done nothing.
#   acceptEdits   -> edits and commands proceed inside the working directory,
#                    and anything outside it is refused by claude itself.
#   bypassPermissions / --dangerously-skip-permissions -> no boundary at all.
# acceptEdits is the default here because it is the weakest mode that still
# lets a detached run finish its work.
DEFAULT_PERMISSION_MODE = "acceptEdits"


def permission_args() -> list[str]:
    """Permission flags for a headless run, from [agents.claude] in config.toml."""
    if yolo_enabled("claude"):
        return ["--dangerously-skip-permissions"]

    mode = agent_setting("claude", "permission_mode", DEFAULT_PERMISSION_MODE)
    args = ["--permission-mode", str(mode)]
    # Comma-separated rather than variadic: `--allowedTools A B` would swallow
    # the positional prompt that follows.
    allowed = agent_list_setting("claude", "allowed_tools")
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]
    disallowed = agent_list_setting("claude", "disallowed_tools")
    if disallowed:
        args += ["--disallowedTools", ",".join(disallowed)]
    return args


class ClaudeHandler:
    name = "claude"

    def is_available(self) -> bool:
        return shutil.which(agent_binary("claude")) is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        """Streaming dispatch — emits on_session_started early.

        `claude -p --output-format stream-json --verbose` produces JSONL
        on stdout where the first line is a `system/init` event carrying
        `session_id`. We catch that within the first poll and fire the
        callback so `playmaker dispatch` persists the id to state.db BEFORE
        the agent finishes — other commands (`get`, `thread`) can locate
        the session within ~1s instead of waiting for the full run.
        """
        full_prompt = self._build_prompt(prompt, files or [])
        # `--verbose` is required by claude-cli when stream-json is used
        # without partial-messages; without it we get a parse-time refusal.
        cmd = [
            agent_binary("claude"),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        cmd += permission_args()
        if model:
            cmd += ["--model", model]
        cmd.append(full_prompt)
        import time as _time
        t0 = _time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )

        agent_session_id: str | None = None
        last_text = ""
        error_text = ""  # error reported in the stream-json result event
        cost_usd: float | None = None
        duration_seconds: float | None = None
        first_lines: list[str] = []  # for diagnostics if no session_id

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

            # Early callback on first event carrying session_id (init).
            if agent_session_id is None and obj.get("session_id"):
                agent_session_id = obj["session_id"]
                if on_session_started is not None:
                    try:
                        on_session_started(agent_session_id)
                    except Exception:
                        pass

            # Capture last assistant text + cost for the result.
            etype = obj.get("type")
            if etype == "assistant":
                content = (obj.get("message") or {}).get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        last_text = block.get("text", "") or last_text
            elif etype == "result":
                last_text = obj.get("result", last_text)
                # claude -p reports failures here (overload, rate-limit, refusal)
                # with is_error=true and exit 1 but an EMPTY stderr — capture the
                # message so the failure isn't surfaced as a blank "failed".
                if obj.get("is_error") or (obj.get("subtype") not in (None, "success")):
                    error_text = (
                        obj.get("result") or obj.get("error") or obj.get("subtype") or ""
                    )
                if obj.get("total_cost_usd") is not None:
                    cost_usd = obj["total_cost_usd"]
                if obj.get("duration_ms") is not None:
                    duration_seconds = obj["duration_ms"] / 1000.0

        stderr_buf = proc.stderr.read() if proc.stderr else ""
        proc.wait()

        if proc.returncode != 0:
            detail = stderr_buf.strip() or error_text or last_text or "".join(first_lines)[:500]
            raise RuntimeError(
                f"claude failed (exit {proc.returncode}): {detail or '(no error output)'}"
            )

        if agent_session_id is None:
            raise RuntimeError(
                f"claude stream-json missing session_id in first events:\n"
                f"{''.join(first_lines)[:500]}"
            )

        if duration_seconds is None:
            duration_seconds = _time.monotonic() - t0

        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=self.find_session_file(agent_session_id, cwd),
            initial_output=last_text,
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
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
        full_prompt = self._build_prompt(prompt, files or [])
        cmd = [
            agent_binary("claude"),
            "-p",
            "--resume",
            agent_session_id,
            "--output-format",
            "json",
        ]
        # See dispatch(): detached resume has no human to approve prompts either.
        cmd += permission_args()
        if model:
            cmd += ["--model", model]
        cmd.append(full_prompt)
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude resume failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude resume returned non-JSON output: {proc.stdout[:500]}"
            ) from e
        # Claude keeps the same session_id on --resume.
        new_session_id = data.get("session_id", agent_session_id)
        if on_session_started is not None:
            try:
                on_session_started(new_session_id)
            except Exception:
                pass
        return DispatchResult(
            agent_session_id=new_session_id,
            cwd=str(cwd),
            session_file=self.find_session_file(new_session_id, cwd),
            initial_output=data.get("result", ""),
            cost_usd=data.get("total_cost_usd"),
            duration_seconds=data.get("duration_ms", 0) / 1000.0,
            exit_code=proc.returncode,
        )

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        cwd_hash = self._hash_cwd(cwd)
        path = Path("~/.claude/projects").expanduser() / cwd_hash / f"{agent_session_id}.jsonl"
        return path if path.exists() else None

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Read Claude jsonl and yield user/assistant turns.

        Each line is a top-level dict with `type` and `timestamp`. Relevant
        types are `user` and `assistant`; `message.content` is either a
        plain string (simple user input) or an array of typed blocks
        (`text`, `thinking`, `tool_use`, `tool_result`).
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
                kind = obj.get("type")
                if kind not in ("user", "assistant"):
                    continue
                msg = obj.get("message") or {}
                role = msg.get("role") or kind
                content_raw = msg.get("content")

                texts: list[str] = []
                tool_calls: list[dict] = []
                tool_results: list[dict] = []
                if isinstance(content_raw, str):
                    texts.append(content_raw)
                elif isinstance(content_raw, list):
                    for block in content_raw:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            texts.append(block.get("text", ""))
                        elif btype == "thinking":
                            # surface thinking for debugging when --include-tools
                            # is on; coach skill controls when to ask for it
                            texts.append(f"[thinking] {block.get('thinking', '')}")
                        elif btype == "tool_use":
                            tool_calls.append(
                                {
                                    "id": block.get("id"),
                                    "name": block.get("name"),
                                    "input": block.get("input"),
                                }
                            )
                        elif btype == "tool_result":
                            content = block.get("content")
                            tool_results.append(
                                {
                                    "tool_use_id": block.get("tool_use_id"),
                                    "content": content
                                    if isinstance(content, str)
                                    else json.dumps(content) if content else "",
                                }
                            )

                ts_raw = obj.get("timestamp")
                ts = None
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                turns.append(
                    Turn(
                        role=role,
                        content="\n".join(t for t in texts if t),
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        timestamp=ts,
                    )
                )
        return turns

    @staticmethod
    def _hash_cwd(cwd: Path) -> str:
        # Empirical: literal `/` -> `-`, applied to the absolute resolved path.
        # e.g. /Users/x/Sites/foo  ->  -Users-x-Sites-foo
        absolute = str(cwd.expanduser().resolve())
        return absolute.replace("/", "-")

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = " ".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

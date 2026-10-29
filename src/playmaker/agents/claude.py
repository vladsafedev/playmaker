"""Claude Code CLI handler.

Empirically:
- oneshot: `claude -p "..."`
- json output: `--output-format json`
- primary cwd: subprocess cwd; `--add-dir` is variadic (multiple dirs) and would
  swallow the positional prompt, so we omit it and rely on subprocess cwd alone
- session file: ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-id>.jsonl
- json result fields: session_id, result, total_cost_usd, duration_ms, usage

This handler is the `claude` lane (the sibling CLI on the Max subscription,
login in the macOS Keychain); `[agents.claude] bare = true` turns it into
`claude --bare`, authenticated only through ANTHROPIC_API_KEY. The
`claude-api` lane subclasses it in claude_sdk.py to run the same agent through
the Claude Agent SDK. The lane key names the `[agents.<key>]` section every
setting is read from.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from playmaker import state
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

# Claude Code's `--effort`: reasoning depth vs speed. No default — unset
# everywhere means the flag is omitted and claude's own default applies.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Per-run override channel for `playmaker dispatch/continue --effort`. The CLI
# sets this for the spawned run only; it is never persisted, so a later
# `continue` without --effort falls back to the config value below. One
# variable serves every claude lane: a process runs one dispatch at a time.
EFFORT_ENV_VAR = "PLAYMAKER_CLAUDE_EFFORT"

# `claude --bare` reads its credentials from exactly this variable (OAuth and
# the Keychain login are never consulted), so a bare lane injects the key here
# — in the child's environment only, never on the command line or in a log.
API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

# Where a bare lane finds its key: `[agents.<lane>] key_source`.
#   keychain -> macOS Keychain generic password, service KEYCHAIN_SERVICE
#   file     -> ~/.playmaker/secrets.env, mode 600, `ANTHROPIC_API_KEY=...`
VALID_KEY_SOURCES = ("keychain", "file")
DEFAULT_KEY_SOURCE = "keychain"
KEYCHAIN_SERVICE = "playmaker-anthropic-api"
KEYCHAIN_ADD_COMMAND = (
    f"security add-generic-password -s {KEYCHAIN_SERVICE} -a playmaker -w"
)
SECRETS_FILENAME = "secrets.env"


def permission_args(agent: str = "claude") -> list[str]:
    """Permission flags for a headless run, from [agents.<agent>] in config.toml."""
    if yolo_enabled(agent):
        return ["--dangerously-skip-permissions"]

    mode = agent_setting(agent, "permission_mode", DEFAULT_PERMISSION_MODE)
    args = ["--permission-mode", str(mode)]
    # Comma-separated rather than variadic: `--allowedTools A B` would swallow
    # the positional prompt that follows.
    allowed = agent_list_setting(agent, "allowed_tools")
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]
    disallowed = agent_list_setting(agent, "disallowed_tools")
    if disallowed:
        args += ["--disallowedTools", ",".join(disallowed)]
    return args


def resolve_effort(agent: str = "claude") -> str | None:
    """Effective effort for this run: per-run override, else config, else none.

    `playmaker dispatch/continue --effort` exports EFFORT_ENV_VAR for the
    spawned run only (never persisted); without it, `[agents.<agent>] effort`
    applies. Raises before any process starts when the value is not one of
    VALID_EFFORTS — the same fail-fast shape as a bad `--model` elsewhere.
    """
    raw = os.environ.get(EFFORT_ENV_VAR)
    if raw is None:
        raw = agent_setting(agent, "effort", None)
    if raw is None:
        return None
    value = str(raw).strip()
    if value not in VALID_EFFORTS:
        raise RuntimeError(
            f"{agent} has no effort {raw!r}; valid efforts: {', '.join(VALID_EFFORTS)} "
            f"([agents.{agent}] effort or --effort)"
        )
    return value


def effort_args(agent: str = "claude") -> list[str]:
    """`["--effort", value]` when configured, else []. Raises on invalid."""
    resolved = resolve_effort(agent)
    return ["--effort", resolved] if resolved is not None else []


def bare_enabled(agent: str, *, default: bool = False) -> bool:
    """Whether this lane runs `claude --bare`: `[agents.<agent>] bare`, else the lane default."""
    value = agent_setting(agent, "bare")
    return default if value is None else bool(value)


def secrets_path() -> Path:
    """The `file` key source: ~/.playmaker/secrets.env."""
    return state.PLAYMAKER_HOME / SECRETS_FILENAME


def _security(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """One `security` call; a failure becomes a RuntimeError that carries no output."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Keychain read of {KEYCHAIN_SERVICE!r} timed out — allow `security` access "
            "in the macOS dialog"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"Keychain read of {KEYCHAIN_SERVICE!r} failed: {type(exc).__name__}"
        ) from exc


def _keychain_api_key() -> str | None:
    """The key from the macOS Keychain, or None when no entry exists.

    Same shape as the Muse Code probe in quotas.py: an existence check first
    (no secret, so no Allow dialog), then the `-w` read with a generous
    timeout. Error messages never carry `security`'s stdout.
    """
    if sys.platform != "darwin" or shutil.which("security") is None:
        return None
    base = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE]
    if _security(base, timeout=10).returncode != 0:
        return None
    proc = _security([*base, "-w"], timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Keychain read of {KEYCHAIN_SERVICE!r} was denied — allow `security` access "
            f"to the item, or re-add it: {KEYCHAIN_ADD_COMMAND}"
        )
    key = proc.stdout.strip()
    return key or None


def _secrets_file_api_key() -> str | None:
    """The key from ~/.playmaker/secrets.env, or None when the file is absent.

    The file must be readable by its owner alone (mode 600): a key in a
    world-readable file is as good as pasted into the config. Lines are
    `NAME=value`; `export NAME=value` and quotes around the value are tolerated.
    """
    path = secrets_path()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(
            f"{path} must be readable by you alone — run: chmod 600 {path}"
        )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == API_KEY_ENV_VAR:
            value = value.strip().strip("'\"")
            return value or None
    return None


def load_api_key(agent: str) -> str:
    """The API key for a bare lane, from `[agents.<agent>] key_source`.

    Raises RuntimeError — without the key or any `security` output in the
    message — when the source is misconfigured or holds no key, so a dispatch
    refuses before any process starts.
    """
    raw = agent_setting(agent, "key_source", DEFAULT_KEY_SOURCE)
    source = str(raw).strip()
    if source not in VALID_KEY_SOURCES:
        raise RuntimeError(
            f"{agent} has no key_source {raw!r}; valid sources: {', '.join(VALID_KEY_SOURCES)} "
            f"([agents.{agent}] key_source)"
        )
    if source == "keychain":
        key = _keychain_api_key()
        if key:
            return key
        platform_note = "" if sys.platform == "darwin" else " (the Keychain is macOS-only)"
        raise RuntimeError(
            f"{agent}: no API key in the macOS Keychain, service {KEYCHAIN_SERVICE!r}"
            f"{platform_note}. Add one: {KEYCHAIN_ADD_COMMAND}  — or set "
            f'[agents.{agent}] key_source = "file" and put {API_KEY_ENV_VAR}=... '
            f"in {secrets_path()} (mode 600)"
        )
    key = _secrets_file_api_key()
    if key:
        return key
    raise RuntimeError(
        f"{agent}: no {API_KEY_ENV_VAR} in {secrets_path()} — create it with mode 600 and one "
        f"line: {API_KEY_ENV_VAR}=sk-ant-..."
    )


class ClaudeHandler:
    """One Claude Code lane.

    `agent_key` is the lane's name and its `[agents.<key>]` config section.
    `bare` is the lane default for `claude --bare`; `[agents.<key>] bare`
    overrides it. A bare lane needs its API key before it can start.
    """

    # `claude -p` takes the prompt as its last argument; a subclass whose
    # executable reads it from stdin sets this instead.
    prompt_on_stdin = False

    def __init__(self, agent_key: str = "claude", *, bare: bool = False) -> None:
        self.name = agent_key
        self._bare_default = bare

    @property
    def bare(self) -> bool:
        return bare_enabled(self.name, default=self._bare_default)

    def is_available(self) -> bool:
        return shutil.which(agent_binary(self.name)) is not None

    def preflight(self) -> None:
        """Raise before any session row exists when the lane cannot start.

        A bare lane without its API key would spawn, fail with claude's own
        auth error and leave a failed row behind; refusing here keeps the
        same fail-fast shape as a bad `--effort`.
        """
        if self.bare:
            load_api_key(self.name)

    def _spawn_env(self) -> dict[str, str] | None:
        """The child's environment: inherited, plus the API key on a bare lane.

        None means "inherit as is" — the subscription lane's environment is
        passed through untouched. The key goes into this mapping only; it is
        never part of the command line, so logs and `ps` never show it.
        """
        if not self.bare:
            return None
        env = dict(os.environ)
        env[API_KEY_ENV_VAR] = load_api_key(self.name)
        return env

    def _base_cmd(self) -> list[str]:
        cmd = [agent_binary(self.name), "-p"]
        if self.bare:
            cmd.append("--bare")
        return cmd

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
        cmd = self._base_cmd() + [
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        cmd += permission_args(self.name)
        if model:
            cmd += ["--model", model]
        cmd += effort_args(self.name)
        if not self.prompt_on_stdin:
            cmd.append(full_prompt)
        env = self._spawn_env()
        import time as _time
        t0 = _time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if self.prompt_on_stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        if self.prompt_on_stdin and proc.stdin is not None:
            try:
                proc.stdin.write(full_prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # the child exited before reading; its stderr says why

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
                f"{self.name} failed (exit {proc.returncode}): {detail or '(no error output)'}"
            )

        if agent_session_id is None:
            raise RuntimeError(
                f"{self.name} stream-json missing session_id in first events:\n"
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
        cmd = self._base_cmd() + [
            "--resume",
            agent_session_id,
            "--output-format",
            "json",
        ]
        # See dispatch(): detached resume has no human to approve prompts either.
        cmd += permission_args(self.name)
        if model:
            cmd += ["--model", model]
        cmd += effort_args(self.name)
        if not self.prompt_on_stdin:
            cmd.append(full_prompt)
        env = self._spawn_env()
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            input=full_prompt if self.prompt_on_stdin else None,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.name} resume failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"{self.name} resume returned non-JSON output: {proc.stdout[:500]}"
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

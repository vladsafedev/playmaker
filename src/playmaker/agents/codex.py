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
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import agent_binary, agent_setting

CODEX_SESSIONS_ROOT = Path("~/.codex/sessions").expanduser()
CODEX_EFFORT_ENV_VAR = "PLAYMAKER_CODEX_EFFORT"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def codex_home() -> Path:
    """The Codex configuration root, honouring the CLI's CODEX_HOME."""
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured or "~/.codex").expanduser()


def configured_model() -> str | None:
    """Codex's configured default model, or None when config is unavailable."""
    try:
        with (codex_home() / "config.toml").open("rb") as fh:
            value = tomllib.load(fh).get("model")
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        return None
    return str(value).strip() if value is not None and str(value).strip() else None


def resolve_effort(
    override: str | None = None, parent_effort: str | None = None
) -> tuple[str | None, str | None]:
    """Resolve Codex effort and report the source used for diagnostics."""
    raw: object | None = override
    source = "--effort" if override is not None else None
    if raw is None:
        env_effort = os.environ.get(CODEX_EFFORT_ENV_VAR, "").strip()
        if env_effort:
            raw = env_effort
            source = CODEX_EFFORT_ENV_VAR
    if raw is None and parent_effort is not None:
        raw = parent_effort
        source = "parent session"
    if raw is None:
        raw = agent_setting("codex", "reasoning_effort")
        if raw is not None:
            source = "[agents.codex] reasoning_effort"
    if raw is None:
        return None, None
    value = str(raw).strip()
    if value not in VALID_EFFORTS:
        raise ValueError(
            f"codex has no reasoning effort {raw!r}; valid: {', '.join(VALID_EFFORTS)} "
            f"(from {source})"
        )
    return value, source


def validate_model_effort(model: str | None, effort: str | None) -> None:
    """Reject a model/effort pair only when Codex's cache describes the model."""
    if model is None or effort is None:
        return
    cache_path = codex_home() / "models_cache.json"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        models = data.get("models")
        if not isinstance(models, list):
            return
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError):
        return
    for entry in models:
        if not isinstance(entry, dict) or entry.get("slug") != model:
            continue
        levels = entry.get("supported_reasoning_levels")
        if not isinstance(levels, list) or not levels:
            return
        if any(
            not isinstance(level, dict)
            or not isinstance(level.get("effort"), str)
            or not level["effort"].strip()
            for level in levels
        ):
            return
        valid = [level["effort"] for level in levels]
        if effort not in valid:
            raise ValueError(
                f"codex model {model} has no reasoning effort {effort!r}; "
                f"valid: {', '.join(valid)} (from {cache_path})"
            )
        return


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def sandbox_args() -> list[str]:
    """Codex's own sandbox policy, from [agents.codex] sandbox in config.toml.

    Unlike claude and agy, codex needs no permission-skipping flag: `codex exec`
    is already non-interactive and confines the model's shell commands with its
    own sandbox. We pass `-s` only when a specific policy was configured;
    otherwise codex's own default applies.
    """
    policy = agent_setting("codex", "sandbox")
    return ["-s", str(policy)] if policy else []


class CodexHandler:
    name = "codex"

    def is_available(self) -> bool:
        return shutil.which(agent_binary("codex")) is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> DispatchResult:
        images, prompt_files = self._split_files(files or [], cwd)
        full_prompt = self._build_prompt(prompt, prompt_files)

        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, prefix="codex-last-"
        ) as tmp:
            last_msg_path = Path(tmp.name)

        cmd = [
            agent_binary("codex"),
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            str(cwd),
            "-o",
            str(last_msg_path),
        ]
        cmd += sandbox_args()
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        cmd += [f"--image={path}" for path in images]
        cmd.append(full_prompt)
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        thread_id, last_message_streamed, error_text, stdout_buf = self._consume_stream(
            proc, on_session_started
        )
        stderr_buf = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        duration = time.monotonic() - t0

        last_message = ""
        if last_msg_path.exists():
            last_message = last_msg_path.read_text(encoding="utf-8").strip()
            last_msg_path.unlink(missing_ok=True)
        if not last_message:
            last_message = last_message_streamed

        # Codex reports invalid-model / auth / server failures via a `turn.failed`
        # or `error` stream event while still exiting 0 and writing an EMPTY
        # last-message file — without this the dispatch would look "done" with no
        # output. Surface it as a failure so the coach reroutes instead of
        # silently getting nothing back. (A non-empty message means the turn
        # recovered, so only raise when we truly have no answer.)
        if error_text and not last_message:
            raise RuntimeError(f"codex turn failed: {error_text}")

        # Codex emits a non-fatal "failed to record rollout items" warning when the
        # agent shuts down; the answer is still written. Gate on the message we
        # actually hold, not on the file — it has been consumed and unlinked above.
        if proc.returncode != 0 and not last_message:
            raise RuntimeError(
                f"codex failed (exit {proc.returncode}): "
                f"{stderr_buf.strip() or error_text or stdout_buf[:500]}"
            )

        if thread_id is None:
            raise RuntimeError(
                f"codex stdout missing thread.started event:\n{stdout_buf[:500]}"
            )

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
    ) -> tuple[str | None, str, str, str]:
        """Read stdout line-by-line, fire callback on thread.started, capture
        the latest agent_message item and any failure event.

        Returns (thread_id, last_message, error_text, raw_stdout). `error_text`
        is the message from a `turn.failed`/`error`/`item.type == "error"` event
        (invalid model, auth, upstream 4xx/5xx) — codex emits these while still
        exiting 0, so callers must inspect it, not just the return code.
        """
        thread_id: str | None = None
        last_message = ""
        error_text = ""
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
            elif etype in ("error", "turn.failed"):
                error_text = CodexHandler._extract_error(obj) or error_text
        return thread_id, last_message, error_text, "".join(buf_parts)

    @staticmethod
    def _extract_error(obj: dict) -> str:
        """Pull a human-readable message out of a codex error/turn.failed event.

        The message field often nests a JSON string
        ({"error":{"message": "..."}}); unwrap it to the innermost message.
        """
        raw = ""
        err = obj.get("error")
        if isinstance(err, dict):
            raw = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            raw = err
        if not raw:
            raw = obj.get("message") or ""
        candidate = raw.strip()
        for _ in range(3):
            if not (candidate.startswith("{") and candidate.endswith("}")):
                break
            try:
                inner = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                break
            nested = inner.get("error") if isinstance(inner, dict) else None
            if isinstance(nested, dict) and nested.get("message"):
                candidate = str(nested["message"]).strip()
            elif isinstance(inner, dict) and inner.get("message"):
                candidate = str(inner["message"]).strip()
            else:
                break
        return candidate

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> DispatchResult:
        images, prompt_files = self._split_files(files or [], cwd)
        full_prompt = self._build_prompt(prompt, prompt_files)

        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, prefix="codex-last-"
        ) as tmp:
            last_msg_path = Path(tmp.name)

        # `codex exec resume` has no --cd; cwd flows through subprocess.
        cmd = [
            agent_binary("codex"),
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "-o",
            str(last_msg_path),
        ]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        cmd += [f"--image={path}" for path in images]
        cmd += [agent_session_id, full_prompt]
        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        thread_id, last_message_streamed, error_text, stdout_buf = self._consume_stream(
            proc, on_session_started
        )
        stderr_buf = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        duration = time.monotonic() - t0

        last_message = ""
        if last_msg_path.exists():
            last_message = last_msg_path.read_text(encoding="utf-8").strip()
            last_msg_path.unlink(missing_ok=True)
        if not last_message:
            last_message = last_message_streamed

        # See dispatch(): codex reports failures via a stream event while exiting 0.
        if error_text and not last_message:
            raise RuntimeError(f"codex resume turn failed: {error_text}")

        if proc.returncode != 0 and not last_message:
            raise RuntimeError(
                f"codex resume failed (exit {proc.returncode}): "
                f"{stderr_buf.strip() or error_text or stdout_buf[:500]}"
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
    def _split_files(files: list[Path], cwd: Path) -> tuple[list[Path], list[Path]]:
        images: list[Path] = []
        prompt_files: list[Path] = []
        for path in files:
            if is_image_file(path):
                absolute = path.expanduser()
                if not absolute.is_absolute():
                    absolute = cwd / absolute
                images.append(Path(os.path.abspath(absolute)))
            else:
                prompt_files.append(path)
        return images, prompt_files

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = "\n".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

"""opencode CLI handler.

One handler, many providers: opencode fronts ~75 backends (z.ai GLM, local
LMStudio/MLX models, OpenAI, Anthropic, …) behind a single `provider/model`
string, so `--model` is the whole routing story — same shape as agy.

Empirically (opencode 1.18.5):
- oneshot: `opencode run --format json "..."`, resume: `... -s <session id>`
- `--format json` writes JSONL to stdout; every event carries `sessionID`
  (`ses_…`), so the id is known from the first line. That line is the first
  `step_start`, which lands once the model starts responding — measured ~10s
  on a cold run, not the ~1s claude's `system/init` gives us, but still well
  before the run finishes.
- permissions: only `--auto` ("auto-approve permissions that are not explicitly
  denied"). Granular control lives in the user's own opencode.json.
- working directory: opencode reads `process.env.PWD`, which `Popen(cwd=…)`
  does NOT update — without help it runs in the *parent's* directory and writes
  files there. We pass `--dir` and fix up PWD.
- transcript: SQLite at $XDG_DATA_HOME/opencode/opencode.db (default
  ~/.local/share/opencode). Tables `session` (incl. directory, cost, tokens),
  `message` and `part`, the latter two holding a JSON `data` blob per row. The
  older storage/*.json tree is legacy and no longer written.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import agent_binary, agent_setting, yolo_enabled
from playmaker.state import PLAYMAKER_HOME


def data_root() -> Path:
    """opencode's data directory. There is no OPENCODE_DATA_DIR; it is XDG."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path("~/.local/share").expanduser()
    return base / "opencode"


def db_path() -> Path:
    return data_root() / "opencode.db"


class OpencodeHandler:
    name = "opencode"

    def is_available(self) -> bool:
        return shutil.which(agent_binary("opencode")) is not None

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def available_models() -> tuple[str, ...]:
        """Model names opencode accepts, from `opencode models` (one per line).

        Note this lists everything opencode knows about, including providers you
        have no credential for — it catches typos, not missing auth. Cached
        per-process; () if the roster can't be read (then validation is skipped
        rather than blocking a dispatch on a probe failure).
        """
        if shutil.which(agent_binary("opencode")) is None:
            return ()
        try:
            proc = subprocess.run(
                [agent_binary("opencode"), "models"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if proc.returncode != 0:
            return ()
        return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())

    def _resolve_model(self, model: str | None) -> str | None:
        """Explicit --model, else [agents.opencode] model, else opencode's own.

        Falling through to opencode's own default is rarely what you want from
        playmaker: it comes from ~/.config/opencode/opencode.json, which is
        whatever the user last picked interactively — often a local model.
        """
        return model or agent_setting("opencode", "model")

    def _validate_model(self, model: str | None) -> None:
        if not model:
            return
        roster = self.available_models()
        if roster and model not in roster:
            raise RuntimeError(
                f"opencode has no model {model!r}. Models are 'provider/model'; "
                f"run `opencode models` for the roster. Available: {', '.join(roster)}"
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
        """Shared oneshot/resume runner.

        stdout is streamed line by line so on_session_started fires on the first
        event rather than at completion; stderr goes to a temp file because a
        second pipe nobody drains can deadlock on a long run.
        """
        effective_model = self._resolve_model(model)
        self._validate_model(effective_model)
        full_prompt = self._build_prompt(prompt, files)

        # --dir is load-bearing, not a nicety: opencode resolves its working
        # directory from process.env.PWD, which Popen(cwd=…) leaves pointing at
        # the parent. Without it a dispatch writes into whatever directory the
        # coach happened to be in. We set both and let them agree.
        cmd = [agent_binary("opencode"), "run", "--format", "json", "--dir", str(cwd)]
        if session_id:
            cmd += ["-s", session_id]
        if effective_model:
            cmd += ["-m", effective_model]
        agent = agent_setting("opencode", "agent")
        if agent:
            cmd += ["--agent", str(agent)]
        variant = agent_setting("opencode", "variant")
        if variant:
            cmd += ["--variant", str(variant)]
        # opencode has no middle tier: like agy there is no per-mode permission
        # flag, so a detached run either auto-approves or comes back having done
        # nothing. Narrow it with `permission` in your own opencode.json.
        if yolo_enabled("opencode", default=True):
            cmd.append("--auto")
        # Prompt goes last, as the sole positional. `-f` is a yargs array flag and
        # `message` is an array positional, so passing both risks the file list
        # swallowing the prompt — we inline @refs instead (see _build_prompt).
        cmd.append(full_prompt)

        t0 = time.monotonic()
        agent_session_id: str | None = None
        texts: dict[str, str] = {}
        error_text = ""
        stream_cost = 0.0
        saw_cost = False
        first_lines: list[str] = []

        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env={**os.environ, "PWD": str(cwd)},
                stdout=subprocess.PIPE,
                stderr=err_fh,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,  # line-buffered
            )
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

                if agent_session_id is None and obj.get("sessionID"):
                    agent_session_id = obj["sessionID"]
                    if on_session_started is not None:
                        try:
                            on_session_started(agent_session_id)
                        except Exception:
                            pass

                etype = obj.get("type")
                part = obj.get("part") or {}
                if etype == "text":
                    # Keyed by part id: a part may be re-emitted as it streams,
                    # and later events carry the fuller text.
                    pid = part.get("id") or str(len(texts))
                    text = part.get("text")
                    if text:
                        texts[pid] = text
                elif etype == "step_finish":
                    cost = part.get("cost")
                    if isinstance(cost, (int, float)):
                        stream_cost += float(cost)
                        saw_cost = True
                elif etype == "error":
                    err = obj.get("error") or {}
                    error_text = (
                        (err.get("data") or {}).get("message") or err.get("name") or ""
                    )

            proc.wait()
            err_fh.seek(0)
            stderr_text = err_fh.read()

        duration = time.monotonic() - t0

        if proc.returncode != 0:
            detail = (
                error_text
                or stderr_text.strip()
                or "".join(first_lines)[:500]
                or "(no error output)"
            )
            raise RuntimeError(f"opencode failed (exit {proc.returncode}): {detail}")

        if agent_session_id is None:
            raise RuntimeError(
                "opencode run emitted no sessionID; first lines:\n"
                f"{''.join(first_lines)[:500] or stderr_text[:500]}"
            )

        session_file = self.find_session_file(agent_session_id, cwd)
        last_text = next((t for t in reversed(list(texts.values())) if t.strip()), "")
        if not last_text:
            last_text = self._last_assistant_text(session_file)

        # `run --format json` can exit before emitting the final step_finish
        # (opencode#26855), so the streamed sum undercounts. opencode's own
        # per-session accounting is written either way — prefer it.
        cost = self._storage_cost(agent_session_id)
        if cost is None:
            cost = stream_cost if saw_cost else None

        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=session_file,
            initial_output=last_text,
            cost_usd=cost,
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        """A stable pointer file for a session that really lives in SQLite.

        opencode ≥1.18 keeps transcripts in `opencode.db`, not one file per
        session, but a handler's contract here is a *path* — and `thread
        --follow` re-reads it and requires it to exist. So playmaker writes a
        small pointer named for the session; `parse_session_file` reads the id
        back off the filename and queries the database live, which keeps
        `--follow` current instead of frozen at dispatch time.
        """
        if not self._session_exists(agent_session_id):
            return None
        pointer = PLAYMAKER_HOME / "opencode" / f"{agent_session_id}.session"
        try:
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(
                f"{agent_session_id}\n"
                f"# pointer only — the transcript lives in {db_path()}\n"
                f"# (tables: session, message, part)\n",
                encoding="utf-8",
            )
        except OSError:
            return None
        return pointer

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Join opencode's `message` rows to their `part` rows, in order.

        `path` is the pointer written by find_session_file; its stem is the
        opencode session id. Both tables keep their payload in a JSON `data`
        column. This is opencode's internal schema, so anything unreadable or
        reshaped degrades to fewer turns rather than raising.
        """
        turns: list[Turn] = []
        session_id = path.stem
        conn = self._connect()
        if conn is None:
            return turns
        try:
            messages = conn.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
                (session_id,),
            ).fetchall()
            parts = conn.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created, id",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return turns
        finally:
            conn.close()

        by_message: dict[str, list[dict]] = {}
        for message_id, raw in parts:
            block = _loads(raw)
            if isinstance(block, dict):
                by_message.setdefault(message_id, []).append(block)

        for message_id, raw in messages:
            msg = _loads(raw)
            if not isinstance(msg, dict):
                continue
            texts: list[str] = []
            tool_calls: list[dict] = []
            tool_results: list[dict] = []

            for part in by_message.get(message_id, []):
                ptype = part.get("type")
                if ptype == "text":
                    texts.append(part.get("text", ""))
                elif ptype == "reasoning":
                    # Same convention as the claude handler: surfaced only when
                    # the caller asked for tool detail.
                    texts.append(f"[thinking] {part.get('text', '')}")
                elif ptype == "tool":
                    state = part.get("state") or {}
                    tool_calls.append(
                        {
                            "id": part.get("callID"),
                            "name": part.get("tool"),
                            "input": state.get("input"),
                        }
                    )
                    output = state.get("output")
                    if output is not None:
                        tool_results.append(
                            {
                                "tool_use_id": part.get("callID"),
                                "content": output
                                if isinstance(output, str)
                                else json.dumps(output),
                            }
                        )
                # step-start / step-finish carry no transcript content

            created = (msg.get("time") or {}).get("created")
            ts = None
            if isinstance(created, (int, float)):
                ts = datetime.fromtimestamp(created / 1000, UTC)

            turns.append(
                Turn(
                    role=msg.get("role") or "assistant",
                    content="\n".join(t for t in texts if t),
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    timestamp=ts,
                )
            )
        return turns

    @staticmethod
    def _connect() -> sqlite3.Connection | None:
        """Read-only handle on opencode.db, or None if it isn't readable."""
        db = db_path()
        if not db.exists():
            return None
        try:
            return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return None

    def _session_exists(self, session_id: str) -> bool:
        conn = self._connect()
        if conn is None:
            return False
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM session WHERE id = ?", (session_id,)
                ).fetchone()
                is not None
            )
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _storage_cost(self, session_id: str) -> float | None:
        """Session cost as opencode accounts for it, or None if unreadable."""
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT cost FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row and isinstance(row[0], (int, float)):
            return float(row[0])
        return None

    def _last_assistant_text(self, session_file: Path | None) -> str:
        """Fallback when the stream yielded no text."""
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
        refs = " ".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"


def _loads(raw: object) -> object | None:
    if not isinstance(raw, (str, bytes)):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

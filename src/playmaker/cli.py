"""Typer CLI app — entry point for `playmaker`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from playmaker import __version__, config, gate, limits, notify, state, watcher, worktree
from playmaker import events as events_mod
from playmaker import (
    queue as queue_mod,
)
from playmaker.agents.claude import EFFORT_ENV_VAR, VALID_EFFORTS
from playmaker.registry import get_handler

app = typer.Typer(
    name="playmaker",
    help="Multi-agent orchestration CLI. Dispatch subtasks to "
    "Claude/Codex/Antigravity and observe.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"playmaker {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="show the installed playmaker version and exit",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Multi-agent orchestration CLI."""


@app.command()
def init() -> None:
    """Bootstrap ~/.playmaker/ structure (config, state.db, dirs)."""
    state.init_db()
    if not state.CONFIG_PATH.exists():
        state.CONFIG_PATH.write_text(_DEFAULT_CONFIG, encoding="utf-8")
    console.print(f"[green]initialized[/green] {state.PLAYMAKER_HOME}")
    console.print(f"  db:     {state.DB_PATH}")
    console.print(f"  logs:   {state.LOGS_DIR}")
    console.print(f"  out:    {state.OUTPUTS_DIR}")
    console.print(f"  config: {state.CONFIG_PATH}")


skill_app = typer.Typer(
    help="Install the bundled playmaker-coach skill for Claude Code.",
    no_args_is_help=True,
)
app.add_typer(skill_app, name="skill")

mod_app = typer.Typer(
    help="Install the bundled pm mod (pane, quotas, wake-up) for Claude Code.",
    no_args_is_help=True,
)
app.add_typer(mod_app, name="mod")

worktree_app = typer.Typer(help="Inspect and remove dispatch worktrees.", no_args_is_help=True)
app.add_typer(worktree_app, name="worktree")
queue_app = typer.Typer(help="Manage per-thread follow-up work.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")

SKILL_NAME = "playmaker-coach"


def _bundled_skill_dir() -> Path:
    """Where the coach skill ships: inside the wheel, or `skills/` in a checkout."""
    packaged = Path(__file__).resolve().parent / "_skills" / SKILL_NAME
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME


@skill_app.command("path")
def skill_path() -> None:
    """Print the path of the bundled skill inside this installation."""
    console.print(str(_bundled_skill_dir()))


@skill_app.command("install")
def skill_install(
    dest: Path = typer.Option(
        Path("~/.claude/skills"),
        "--dir",
        help="skills directory to install into",
    ),
    force: bool = typer.Option(False, "--force", help="overwrite an existing copy"),
) -> None:
    """Copy the playmaker-coach skill into your Claude Code skills directory."""
    source_dir = _bundled_skill_dir()
    source = source_dir / "SKILL.md"
    if not source.exists():
        err_console.print(f"[red]bundled skill not found at {source}[/red]")
        raise typer.Exit(1)

    target_dir = dest.expanduser() / SKILL_NAME
    target = target_dir / "SKILL.md"
    if target.exists() and not force:
        err_console.print(f"[yellow]{target} already exists[/yellow] — pass --force to overwrite")
        raise typer.Exit(1)

    # The skill is a directory, not a file: SKILL.md holds the protocol, references/ the parts the
    # coach loads on demand, scripts/ the review fan-out. Copy files in, but never delete anything
    # already there — a user's own additions alongside the bundle are theirs to keep.
    written = 0
    for src in sorted(p for p in source_dir.rglob("*") if p.is_file()):
        if "__pycache__" in src.parts or src.name == ".DS_Store":
            continue
        dst = target_dir / src.relative_to(source_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        dst.chmod(src.stat().st_mode & 0o777)  # keep the +x on bundled scripts
        written += 1

    console.print(f"[green]installed[/green] {target_dir} [dim]({written} files)[/dim]")
    console.print("  Personal routing rules belong in ~/.playmaker/policy.md, not in SKILL.md —")
    console.print("  the skill reads it at plan time, so --force never clobbers your policy.")
    console.print("  Start a new Claude Code session and give it a multi-part task.")


MOD_NAME = "pm"
# The marketplace `mod install` writes around its copy. Its own name, so it never
# collides with the repository's `playmaker` marketplace a checkout may have added.
MOD_MARKETPLACE = "playmaker-local"


def _bundled_mod_dir() -> Path:
    """Where the pm mod ships: inside the wheel, or `mods/` in a checkout."""
    packaged = Path(__file__).resolve().parent / "_mods" / MOD_NAME
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "mods" / MOD_NAME


@mod_app.command("path")
def mod_path() -> None:
    """Print the path of the bundled mod inside this installation."""
    console.print(str(_bundled_mod_dir()))


def _claude_plugin(args: list[str]) -> tuple[bool, str]:
    """Run `claude plugin <args>`; (ok, output). A missing `claude` is not ok."""
    try:
        ran = subprocess.run(
            ["claude", "plugin", *args], capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return ran.returncode == 0, (ran.stdout + ran.stderr).strip()


@mod_app.command("install")
def mod_install(
    dest: Path = typer.Option(
        Path("~/.playmaker/mods"),
        "--dir",
        help="folder to keep the mod in; it becomes a local marketplace",
    ),
    force: bool = typer.Option(False, "--force", help="overwrite an existing copy"),
    register: bool = typer.Option(
        True,
        "--register/--no-register",
        help="add the marketplace and install the plugin with `claude plugin`",
    ),
) -> None:
    """Copy the pm mod into a folder and install it as a Claude Code plugin."""
    source_dir = _bundled_mod_dir()
    if not (source_dir / ".claude-plugin" / "plugin.json").is_file():
        err_console.print(f"[red]bundled mod not found at {source_dir}[/red]")
        raise typer.Exit(1)

    root = dest.expanduser()
    target_dir = root / MOD_NAME
    if (target_dir / ".claude-plugin" / "plugin.json").exists() and not force:
        err_console.print(
            f"[yellow]{target_dir} already exists[/yellow] — pass --force to overwrite"
        )
        raise typer.Exit(1)

    # Files the engine writes beside a loaded mod (its type declarations) are its
    # own; the bundle never carries them, so a copy over them leaves them be.
    written = 0
    for src in sorted(p for p in source_dir.rglob("*") if p.is_file()):
        if "__pycache__" in src.parts or src.name == ".DS_Store":
            continue
        dst = target_dir / src.relative_to(source_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        written += 1

    marketplace = root / ".claude-plugin" / "marketplace.json"
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": MOD_MARKETPLACE,
                "owner": {"name": "playmaker"},
                "plugins": [{"name": MOD_NAME, "source": f"./{MOD_NAME}"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    console.print(f"[green]copied[/green] {target_dir} [dim]({written} files)[/dim]")

    steps = [
        ["marketplace", "add", str(root)],
        ["install", f"{MOD_NAME}@{MOD_MARKETPLACE}"],
    ]
    if not register:
        console.print("  Register it with:")
        for step in steps:
            console.print(f"    claude plugin {' '.join(step)}")
        return
    for step in steps:
        ok, out = _claude_plugin(step)
        # Adding a marketplace that is already there is not a failure here.
        if not ok and not (step[0] == "marketplace" and "already" in out.lower()):
            err_console.print(f"[red]claude plugin {' '.join(step)} failed[/red]: {out}")
            console.print("  Run the remaining step yourself once `claude` is on PATH:")
            for rest in steps[steps.index(step) :]:
                console.print(f"    claude plugin {' '.join(rest)}")
            raise typer.Exit(1)
    console.print(f"[green]installed[/green] {MOD_NAME}@{MOD_MARKETPLACE}")
    console.print("  New Claude Code sessions load it; in a running one, type /reload-plugins.")

def _apply_effort_override(agent: str, effort: str | None) -> None:
    """Route `--effort` to the claude lane for this run only.

    The value travels in EFFORT_ENV_VAR, which ClaudeHandler reads before
    each spawn — nothing is persisted, so a later `continue` without --effort
    falls back to [agents.claude] effort. Other lanes accept the flag so
    scripts can pass it uniformly, and ignore it with one stderr line.
    """
    if effort is None:
        return
    if agent != "claude":
        err_console.print(f"--effort is forwarded to the claude lane only; ignored for {agent}")
        return
    if effort not in VALID_EFFORTS:
        err_console.print(
            f"claude has no effort {effort!r}; valid efforts: {', '.join(VALID_EFFORTS)} "
            "([agents.claude] effort or --effort)"
        )
        raise typer.Exit(2)
    os.environ[EFFORT_ENV_VAR] = effort


FALLBACK_REASONS = frozenset({"quota", "auth", "binary", "model"})
DEFAULT_FALLBACK_WINDOW = 120
DEFAULT_EVENTS_INTERVAL = 2.0
DEFAULT_EVENTS_LAST = 50


def _parse_fallback_lanes(raw: str | None) -> list[str]:
    """Split and validate `--fallback LANE,LANE`; unknown lane exits 1."""
    if raw is None or not raw.strip():
        return []
    lanes = [lane.strip() for lane in raw.split(",") if lane.strip()]
    for lane in lanes:
        try:
            get_handler(lane)
        except KeyError as exc:
            err_console.print(f"[red]unknown fallback lane:[/red] {exc}")
            raise typer.Exit(1) from exc
    return lanes


def _spawn_detached(sid: str) -> int:
    """Run `python -m playmaker _run-detached <sid>` in a new session; mark running."""
    log_path = state.LOGS_DIR / f"{sid}.log"
    log_fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "playmaker", "_run-detached", sid],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fh.close()
    state.update_session(sid, status="running", pid=proc.pid)
    return proc.pid


def _resolve_prompt(prompt: str | None, prompt_file: Path | None) -> str:
    """Exactly one of --prompt / --prompt-file; the file's UTF-8 content becomes the prompt.

    A prompt travelling in a shell argument executes backticks in the caller's
    shell and hangs the CLI on long text — reading it from a file avoids both.
    """
    if prompt is not None and prompt_file is not None:
        err_console.print("[red]pass either --prompt or --prompt-file, not both[/red]")
        raise typer.Exit(2)
    if prompt is not None:
        return prompt
    if prompt_file is None:
        err_console.print("[red]pass a prompt: --prompt TEXT or --prompt-file PATH[/red]")
        raise typer.Exit(2)
    try:
        text = prompt_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        err_console.print(f"[red]cannot read --prompt-file {prompt_file}: {exc}[/red]")
        raise typer.Exit(2) from exc
    if not text.strip():
        err_console.print(f"[red]--prompt-file {prompt_file} is empty[/red]")
        raise typer.Exit(2)
    return text


def _refuse_if_limited() -> None:
    """Refuse (exit 3) when [limits] min_free_ram_gb says no.

    max_parallel is enforced atomically inside insert_session: a separate
    pre-count and insert would let two simultaneous dispatches both see the
    same running count and both slip past the limit. evaluate() still reports
    it — the `limits` command shows the prediction — but only the RAM refusal
    can stop a dispatch here.
    """
    decision = limits.evaluate()
    for refusal in decision.refusals:
        if refusal.startswith("max_parallel="):
            continue
        err_console.print(f"[red]{refusal}[/red]")
        raise typer.Exit(3)


def _max_active() -> int:
    """[limits] max_parallel as a positive int; 0 = the guard is off."""
    try:
        return max(0, int(config.setting("limits", "max_parallel", 0)))
    except (TypeError, ValueError):
        return 0


def _parallel_refusal(exc: state.LimitExceeded, max_active: int) -> str:
    """The max_parallel refusal: running ids (8 chars), agents, ages, --force hint."""
    lines = [
        f"max_parallel={max_active} reached: {len(exc.running_ids)} session(s) running or pending"
    ]
    for sid in exc.running_ids:
        row = state.get_session(sid) or {}
        started = limits._age(row.get("started_at") or "")
        lines.append(f"  {sid[:8]}  {row.get('agent') or '?'}  started {started}")
    lines.append(limits.FORCE_HINT)
    return "\n".join(lines)


@app.command()
def dispatch(
    agent: str = typer.Argument(
        ..., help="agent name (claude|codex|agy|gemini|kimi|muse|opencode)"
    ),
    prompt: str | None = typer.Option(
        None, "--prompt", "-p", help="initial prompt (or pass --prompt-file)"
    ),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="read the prompt from a UTF-8 file instead of --prompt"
    ),
    cwd: Path = typer.Option(
        Path.cwd(),
        "--cwd",
        help="working directory for the agent (defaults to current dir)",
    ),
    files: list[Path] | None = typer.Option(
        None, "--files", "-f", help="files to attach to prompt"
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="forwarded to the agent CLI's --model (e.g. claude 'opus'/'sonnet', "
        "agy 'claude-opus-4-6-thinking' — exact names from `agy models`; "
        "opencode 'provider/model' e.g. 'zai-coding-plan/glm-5.2', from "
        "`opencode models`); omitted = agent default, which is the safe choice "
        "for codex",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="forwarded to the claude CLI's --effort (low|medium|high|xhigh|max); "
        "overrides [agents.claude] effort for this run; accepted and ignored "
        "for other agents",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="block until the agent finishes and print final output (default is detached)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="bypass the [limits] max_parallel and min_free_ram_gb guards",
    ),
    parent: str | None = typer.Option(
        None, "--parent", help="parent session id (for delegation tree)"
    ),
    batch: str | None = typer.Option(
        None,
        "--batch",
        help="batch label: pass the same value to every dispatch in one fan-out. "
        "Per-dispatch success pings are suppressed; one summary fires when the "
        "whole batch finishes. Failures still ping immediately.",
    ),
    expect_changes: bool | None = typer.Option(
        None,
        "--expect-changes",
        help="mark this as a write task even if the prompt heuristic does not",
    ),
    read_only: bool | None = typer.Option(
        None,
        "--read-only",
        help="disable the write-task check for recon or answer-only work",
    ),
    worktree_name: str | None = typer.Option(
        None, "--worktree", help="isolate this dispatch in wp/NAME"
    ),
    worktree_base: str | None = typer.Option(
        None, "--worktree-base", help="base ref for a new worktree"
    ),
    gate_cmd: str | None = typer.Option(
        None, "--gate", help="command to run after a successful agent run"
    ),
    fallback: str | None = typer.Option(
        None,
        "--fallback",
        help="comma-separated lanes to reroute this prompt to when the dispatch "
        "fails fast: a quota/auth/binary/model refusal, or an unknown failure "
        "within [fallback] window seconds of start",
    ),
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable result"),
) -> None:
    """Run an agent non-interactively. Detached by default — prints session id
    and returns immediately. Use --sync to block and print the final answer."""
    state.init_db()
    fallback_lanes = _parse_fallback_lanes(fallback)
    prompt = _resolve_prompt(prompt, prompt_file)
    handler = get_handler(agent)
    if not handler.is_available():
        err_console.print(_unavailable(agent))
        raise typer.Exit(1)
    _apply_effort_override(agent, effort)
    if not force:
        _refuse_if_limited()

    cwd_resolved = cwd.expanduser().resolve()
    explicit_expectation = _expectation_from_flags(expect_changes, read_only)
    try:
        sid = state.insert_session(
            agent=agent,
            prompt=prompt,
            cwd=str(cwd_resolved),
            files=[str(f) for f in (files or [])],
            parent_id=parent,
            model=model,
            batch_id=batch,
            expect_changes=state.expects_changes(prompt, explicit_expectation),
            gate_cmd=gate_cmd,
            max_active=None if force else _max_active(),
        )
    except state.LimitExceeded as exc:
        max_active = _max_active()
        err_console.print(f"[red]{_parallel_refusal(exc, max_active)}[/red]")
        raise typer.Exit(3) from exc
    if fallback_lanes:
        state.update_session(sid, fallback_lanes=json.dumps(fallback_lanes))

    if worktree_name:
        try:
            prepared = worktree.prepare(
                cwd_resolved,
                worktree_name,
                worktree_base,
                state.LOGS_DIR / f"{sid}.log",
                state.worktree_setup_timeout_seconds(),
            )
        except worktree.WorktreeError as exc:
            state.update_session(
                sid,
                status="failed",
                finished_at=state.now_iso(),
                exit_code=exc.exit_code,
                fail_reason="worktree_setup",
            )
            err_console.print(
                f"[red]worktree setup failed:[/red] {exc} "
                f"(exit {exc.exit_code}; log: {state.LOGS_DIR / f'{sid}.log'})"
            )
            raise typer.Exit(1) from exc
        cwd_resolved = prepared.path
        state.update_session(
            sid,
            cwd=str(prepared.path),
            worktree_path=str(prepared.path),
            worktree_branch=prepared.branch,
            repo_root=str(prepared.root),
        )

    if sync:
        state.update_session(sid, status="running", pid=os.getpid())
        _run_dispatch(sid)
        return

    pid = _spawn_detached(sid)
    if json_out:
        payload = {"session_id": sid, "pid": pid, "status": "running"}
        row = state.get_session(sid) or {}
        payload.update({k: row.get(k) for k in ("worktree_path", "worktree_branch", "repo_root")})
        typer.echo(json.dumps(payload))
    else:
        console.print(f"[dim]session: {sid}  pid: {pid}  (detached)[/dim]")


def _run_dispatch(sid: str, *, can_fallback: bool = False) -> None:
    """Execute the dispatch for a pending session and update its row.

    `can_fallback` is True only on the detached path: a `--sync` dispatch runs
    in the caller's foreground process, where silently spawning a reroute would
    outlive the command the user is watching.
    """
    row = state.get_session(sid)
    if row is None:
        err_console.print(f"[red]session {sid!r} vanished[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])
    cwd = Path(row["cwd"])
    files = [Path(p) for p in json.loads(row["files"] or "[]")]
    before_snapshot = state.take_working_tree_snapshot(cwd)
    state.update_session(sid, pre_tree_hash=before_snapshot.tree_hash)

    def _on_session_started(agent_session_id: str) -> None:
        # Persist the id immediately so other commands (`get`, `thread`) can
        # locate the session before the agent finishes.
        state.update_session(sid, agent_session_id=agent_session_id)

    # Pre-populated agent_session_id is the marker that this row is a resume
    # of an existing agent thread (set by `continue` before spawning).
    resume_target = row.get("agent_session_id")
    model = row.get("model")
    try:
        if resume_target:
            result = handler.resume(
                row["prompt"],
                cwd,
                resume_target,
                files,
                on_session_started=_on_session_started,
                model=model,
            )
        else:
            result = handler.dispatch(
                row["prompt"],
                cwd,
                files,
                on_session_started=_on_session_started,
                model=model,
            )
    except Exception as exc:
        before_snapshot.cleanup()
        reason = events_mod.classify_failure(
            str(exc), events_mod.read_log_tail(state.LOGS_DIR / f"{sid}.log")
        )
        state.update_session(
            sid, status="failed", finished_at=state.now_iso(), exit_code=1, fail_reason=reason
        )
        err_console.print(f"[red]dispatch failed:[/red] {exc}")
        # Failures always ping immediately and loudly (Basso), even inside a
        # batch — they're the actionable event. Click opens the log.
        notify.notify(
            f"playmaker — {row['agent']} FAILED",
            _one_line(str(exc), 120),
            sound_name="Basso",
            open_path=str(state.LOGS_DIR / f"{sid}.log"),
            group=f"playmaker-fail-{sid}",
        )
        if can_fallback:
            _maybe_fallback(sid, row, reason)
        _maybe_finalize_batch(row.get("batch_id"))
        root = state.thread_root(sid)
        if queue_mod.queued_count(root):
            state.add_event(sid, "queue_blocked", detail={"thread_id": root, "reason": reason})
        raise typer.Exit(1) from exc

    try:
        output_path = _write_output(sid, result.initial_output)
        after_snapshot = state.take_working_tree_snapshot(cwd, with_marker=False)
        files_changed = state.count_working_tree_changes(before_snapshot, after_snapshot)
        final_status = (
            "no_changes" if files_changed == 0 and bool(row.get("expect_changes")) else "done"
        )

        state.update_session(
            sid,
            status=final_status,
            finished_at=state.now_iso(),
            agent_session_id=result.agent_session_id,
            session_file_path=str(result.session_file) if result.session_file else None,
            output_path=str(output_path),
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            exit_code=result.exit_code,
            files_changed=files_changed,
            post_tree_hash=after_snapshot.tree_hash,
        )

        if row.get("gate_cmd") and final_status in {"done", "no_changes"}:
            gate_result = gate.run(
                row["gate_cmd"],
                cwd,
                state.LOGS_DIR / f"{sid}.gate.log",
                _parse_duration(config.setting("gate", "timeout", "20m"), 1200),
            )
            state.update_session(
                sid,
                gate_status=gate_result.status,
                gate_exit_code=gate_result.exit_code,
                gate_log_path=str(gate_result.log_path),
            )
            if gate_result.status != "pass":
                notify.notify(
                    f"playmaker — gate {gate_result.status}",
                    f"{row['agent']} gate exited {gate_result.exit_code}",
                    sound_name="Basso",
                    open_path=str(gate_result.log_path),
                    group=f"playmaker-gate-{sid}",
                )

        if final_status == "no_changes":
            message = f"done but wrote 0 files in {cwd}"
            notify.notify(
                f"playmaker — {row['agent']} {message}",
                message,
                sound_name="Basso",
                open_path=str(output_path),
                group=f"playmaker-no-changes-{sid}",
            )
        # In a batch, stay quiet per-dispatch — one summary fires when the
        # whole batch drains (see _maybe_finalize_batch). no_changes is an
        # exception like a failure: it pings immediately and loudly above.
        elif not row.get("batch_id"):
            notify.notify(
                f"playmaker — {row['agent']} done",
                _one_line(result.initial_output),
                sound_name="Blow",
                open_path=str(output_path),
                group=f"playmaker-{sid}",
            )
        _maybe_finalize_batch(row.get("batch_id"))
        # This is intentionally after gate/event persistence: a queue child sees
        # a completely finished parent, even when the gate itself failed.
        queue_mod.deliver_next(state.thread_root(sid), sid, _spawn_detached)
    finally:
        before_snapshot.cleanup()

    console.print(f"[dim]session: {sid}  agent_session: {result.agent_session_id}[/dim]")
    typer.echo(result.initial_output)


def _write_output(sid: str, text: str) -> Path:
    """Persist an agent's final output. Most outputs are Markdown, so use `.md`
    (renders in Quick Look / editors); detect genuine JSON and use `.json`."""
    ext = "md"
    stripped = (text or "").strip()
    if stripped[:1] in "{[":
        try:
            json.loads(stripped)
            ext = "json"
        except ValueError:
            pass
    path = state.OUTPUTS_DIR / f"{sid}.{ext}"
    path.write_text(text or "", encoding="utf-8")
    return path


def _one_line(text: str, limit: int = 90) -> str:
    """Collapse whitespace, drop markdown markers, truncate (char-safe)."""
    t = " ".join((text or "").split())
    for ch in ("`", "*", "#", ">", "_", "~"):
        t = t.replace(ch, "")
    t = " ".join(t.split())
    return (t[:limit] + "…") if len(t) > limit else t


def _batch_slug(batch_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in batch_id)[:60]


def _within_fallback_window(started_at: str | None) -> bool:
    """Whether an unknown failure happened soon enough to look like a refusal."""
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    window = _parse_duration(
        config.setting("fallback", "window", DEFAULT_FALLBACK_WINDOW),
        float(DEFAULT_FALLBACK_WINDOW),
    )
    return (datetime.now(UTC) - started).total_seconds() <= window


def _maybe_fallback(sid: str, row: dict, reason: str) -> None:
    """Reroute a failed dispatch to the next lane on its fallback list.

    Only fast refusals reroute: a classified quota/auth/binary/model failure,
    or an unknown one inside the fallback window — a worker that produced real
    output and then died should not silently run again on another lane.
    """
    lanes = json.loads(row.get("fallback_lanes") or "[]")
    if not lanes:
        return
    if reason not in FALLBACK_REASONS and not _within_fallback_window(row.get("started_at")):
        return
    lane, remaining = lanes[0], lanes[1:]
    new_sid = state.insert_session(
        agent=lane,
        prompt=row["prompt"],
        cwd=row["cwd"],
        files=json.loads(row["files"] or "[]"),
        parent_id=sid,
        model=None,
        batch_id=row.get("batch_id"),
        expect_changes=(
            bool(row["expect_changes"]) if row.get("expect_changes") is not None else None
        ),
    )
    if remaining:
        state.update_session(new_sid, fallback_lanes=json.dumps(remaining))
    state.update_session(new_sid, fallback_of=sid)
    new_pid = _spawn_detached(new_sid)
    state.update_session(sid, fell_back_to=new_sid)
    state.add_event(
        sid, "fallback", detail={"from": sid, "to": new_sid, "reason": reason, "lane": lane}
    )
    notify.notify(
        f"playmaker — {row['agent']} rerouted",
        f"rerouted to {lane} ({reason})",
        sound_name="Blow",
        group=f"playmaker-fallback-{new_sid}",
    )
    console.print(f"[dim]fallback: {sid} → {new_sid} ({lane}, pid {new_pid})[/dim]")


def _maybe_finalize_batch(batch_id: str | None) -> None:
    """Fire one summary notification when every session in a batch is terminal.

    Cross-process safe: each detached dispatch calls this on completion; only
    the finisher that wins the claim in state.db actually notifies. The claim
    also releases the label — dispatching `--batch dash` again tomorrow is a
    new fan-out with its own summary, not a repeat of this one.
    """
    if not batch_id:
        return
    siblings = state.list_batch(batch_id)
    if not siblings:
        return
    if any(s["status"] not in state.TERMINAL_STATUSES for s in siblings):
        return  # not the last to finish

    if not state.claim_batch([s["id"] for s in siblings]):
        return  # another finisher already fired the summary

    state.add_event(
        None,
        "batch",
        status="done",
        batch_id=batch_id,
        detail={
            "total": len(siblings),
            "done": sum(1 for s in siblings if s["status"] == "done"),
            "failed": sum(1 for s in siblings if s["status"] == "failed"),
            "no_changes": sum(1 for s in siblings if s["status"] == "no_changes"),
        },
    )

    ok = [s for s in siblings if s["status"] == "done"]
    marks = " · ".join(_batch_status_mark(s) for s in siblings)
    combined = _render_batch_file(batch_id, siblings)
    notify.notify(
        "playmaker — batch done",
        f"{len(ok)}/{len(siblings)} done · {marks}",
        sound_name="Blow" if len(ok) == len(siblings) else "Basso",
        open_path=str(combined) if combined else None,
        group=f"playmaker-batch-{_batch_slug(batch_id)}",
    )


def _batch_status_mark(session: dict) -> str:
    if session["status"] == "done":
        return f"{session['agent']} ✓"
    if session["status"] == "no_changes":
        return f"{session['agent']} ⚠ no_changes"
    return f"{session['agent']} ✗"


def _render_batch_file(batch_id: str, siblings: list) -> Path | None:
    """Write a combined markdown view of all batch outputs, for review.

    It lands in `outputs/` beside the per-session files it quotes: it is what
    the batch notification opens on click, and its name is derived from a label
    the user chose, which is not something to hand to a shared /tmp.
    """
    lines = [f"# playmaker batch: {batch_id}", ""]
    for s in siblings:
        lines.append(f"## {s['agent']} — {s['status']}  ({s['id'][:8]})")
        out_path = s.get("output_path")
        if not out_path:
            matches = sorted(state.OUTPUTS_DIR.glob(f"{s['id']}.*"))
            out_path = str(matches[0]) if matches else str(state.OUTPUTS_DIR / f"{s['id']}.md")
        try:
            content = Path(out_path).read_text(encoding="utf-8").strip()
        except OSError:
            content = "_(no output captured — see `playmaker logs " + s["id"][:8] + "`)_"
        lines += ["", content or "_(empty)_", ""]
    target = state.OUTPUTS_DIR / f"batch-{_batch_slug(batch_id)}.md"
    try:
        target.write_text("\n".join(lines), encoding="utf-8")
        return target
    except OSError:
        return None


@app.command("_run-detached", hidden=True)
def _run_detached(session_id: str) -> None:
    """Internal: run a pre-inserted session in the background."""
    state.init_db()
    _run_dispatch(session_id, can_fallback=True)


@app.command("continue")
def continue_(
    session_id: str = typer.Argument(..., help="existing session id (or unique prefix) to resume"),
    prompt: str | None = typer.Option(
        None, "--prompt", "-p", help="follow-up prompt (or pass --prompt-file)"
    ),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="read the follow-up prompt from a UTF-8 file"
    ),
    cwd: Path | None = typer.Option(
        None,
        "--cwd",
        help="override working directory (defaults to the parent session's cwd)",
    ),
    files: list[Path] | None = typer.Option(
        None, "--files", "-f", help="files to attach to prompt"
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="override the model for this turn; defaults to the parent session's model",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="override the effort for this turn; defaults to [agents.claude] effort",
    ),
    sync: bool = typer.Option(
        False, "--sync", help="block until done and print final output (default is detached)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="bypass the [limits] max_parallel and min_free_ram_gb guards",
    ),
    expect_changes: bool | None = typer.Option(
        None,
        "--expect-changes",
        help="mark this follow-up as a write task",
    ),
    read_only: bool | None = typer.Option(
        None,
        "--read-only",
        help="disable the write-task check for this follow-up",
    ),
    gate_cmd: str | None = typer.Option(None, "--gate", help="command to run after this follow-up"),
    after: bool = typer.Option(False, "--after", help="queue this follow-up even if idle"),
    now: bool = typer.Option(
        False, "--now", help="attempt an immediate resume even if the thread is live"
    ),
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable result"),
) -> None:
    """Resume a previous agent session with a follow-up prompt — preserves the
    sub-agent's prior reasoning and tool history. Use this for incremental
    feedback; only fall back to a fresh `dispatch` if context is stale."""
    state.init_db()
    prompt = _resolve_prompt(prompt, prompt_file)
    if after and now:
        err_console.print("[red]--after and --now cannot be used together[/red]")
        raise typer.Exit(2)
    parent = state.get_session(session_id)
    if parent is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    root = state.thread_root(parent["id"])
    thread_rows = state.thread_sessions(root)
    live = any(item["status"] in {"pending", "running"} for item in thread_rows)

    handler = get_handler(parent["agent"])
    if not handler.is_available():
        err_console.print(_unavailable(parent["agent"]))
        raise typer.Exit(1)
    _apply_effort_override(parent["agent"], effort)
    if not force:
        _refuse_if_limited()

    cwd_resolved = (cwd or Path(parent["cwd"])).expanduser().resolve()
    effective_model = model if model is not None else parent.get("model")
    explicit_expectation = _expectation_from_flags(expect_changes, read_only)
    inherited_expectation = parent.get("expect_changes")
    session_expects_changes = (
        bool(inherited_expectation)
        if explicit_expectation is None and inherited_expectation is not None
        else state.expects_changes(prompt, explicit_expectation)
    )
    if after or (live and not now):
        if sync:
            err_console.print("[red]cannot --sync a queued follow-up[/red]")
            raise typer.Exit(2)
        root_row = state.get_session(root) or parent
        item = queue_mod.enqueue(
            root,
            prompt,
            cwd=str(cwd_resolved) if cwd is not None else None,
            files=[str(f) for f in (files or [])],
            gate_cmd=gate_cmd if gate_cmd is not None else root_row.get("gate_cmd"),
            model=effective_model,
            expect_changes=session_expects_changes,
        )
        if json_out:
            typer.echo(json.dumps({"queued": True, "item": item["id"], "thread_id": root}))
        else:
            console.print(
                f"queued #{item['id']} in thread {root[:8]} — delivered when the worker finishes"
            )
        return
    parent_agent_session_id = parent.get("agent_session_id")
    if not parent_agent_session_id:
        err_console.print(
            f"[red]parent session {parent['id'][:8]} has no agent_session_id yet "
            f"(status={parent['status']}); cannot resume[/red]"
        )
        raise typer.Exit(1) from None
    if live and now:
        err_console.print(
            "[yellow]warning: a live codex thread may refuse a second writer[/yellow]"
        )
    parent_worktree = parent.get("worktree_path")
    inherits_worktree = bool(parent_worktree) and cwd_resolved.is_relative_to(Path(parent_worktree))

    # New playmaker session that targets the parent's live agent thread.
    try:
        sid = state.insert_session(
            agent=parent["agent"],
            prompt=prompt,
            cwd=str(cwd_resolved),
            files=[str(f) for f in (files or [])],
            parent_id=parent["id"],
            model=effective_model,
            expect_changes=session_expects_changes,
            worktree_path=parent.get("worktree_path") if inherits_worktree else None,
            worktree_branch=parent.get("worktree_branch") if inherits_worktree else None,
            repo_root=parent.get("repo_root") if inherits_worktree else None,
            gate_cmd=gate_cmd,
            max_active=None if force else _max_active(),
        )
    except state.LimitExceeded as exc:
        err_console.print(f"[red]{_parallel_refusal(exc, _max_active())}[/red]")
        raise typer.Exit(3) from exc
    # Pre-populating agent_session_id flips _run_dispatch into resume mode.
    state.update_session(sid, agent_session_id=parent_agent_session_id)

    if sync:
        state.update_session(sid, status="running", pid=os.getpid())
        _run_dispatch(sid)
        return

    pid = _spawn_detached(sid)
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "session_id": sid,
                    "pid": pid,
                    "status": "running",
                    "resumes": parent_agent_session_id,
                    "parent_id": parent["id"],
                }
            )
        )
    else:
        console.print(
            f"[dim]session: {sid}  pid: {pid}  "
            f"resumes {parent['agent']} thread {parent_agent_session_id[:8]}  (detached)[/dim]"
        )


def _queue_event(item: dict, kind: str) -> None:
    state.add_event(item["thread_id"], kind, detail={"item": item["id"]})


@queue_app.command("ls")
def queue_ls(
    thread: str | None = typer.Option(None, "--thread"),
    all_: bool = typer.Option(False, "--all"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    state.init_db()
    try:
        root = state.thread_root(thread) if thread else None
    except ValueError:
        err_console.print(f"[red]session {thread!r} not found[/red]")
        raise typer.Exit(1) from None
    rows = queue_mod.list_items(root, all_=all_)
    if json_out:
        typer.echo(json.dumps(rows))
        return
    table = Table("item", "thread", "pos", "status", "created", "gate", "prompt", "delivered_as")
    for r in rows:
        table.add_row(
            str(r["id"]),
            r["thread_id"][:8],
            str(r["position"]),
            r["status"],
            r["created_at"],
            "✓" if r["gate_cmd"] else "–",
            _one_line(r["prompt"], 60),
            (r["delivered_as"] or "")[:8],
        )
    console.print(table)


@queue_app.command("show")
def queue_show(item: int, json_out: bool = typer.Option(False, "--json")) -> None:
    state.init_db()
    row = queue_mod.get(item)
    if not row:
        err_console.print(f"[red]queue item #{item} not found[/red]")
        raise typer.Exit(1) from None
    typer.echo(json.dumps(row) if json_out else "\n".join(f"{k}: {v}" for k, v in row.items()))


@queue_app.command("add")
def queue_add(
    session_id: str,
    prompt: str | None = typer.Option(None, "--prompt", "-p"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    files: list[Path] | None = typer.Option(None, "--files", "-f"),
    gate_cmd: str | None = typer.Option(None, "--gate"),
    model: str | None = typer.Option(None, "--model"),
    expect_changes: bool = typer.Option(False, "--expect-changes"),
    read_only: bool = typer.Option(False, "--read-only"),
    first: bool = typer.Option(False, "--first"),
    position: int | None = typer.Option(None, "--position"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    state.init_db()
    prompt = _resolve_prompt(prompt, prompt_file)
    if first and position:
        raise typer.BadParameter("use only one of --first and --position")
    if position is not None and position < 1:
        raise typer.BadParameter("--position must be at least 1")
    row = state.get_session(session_id)
    if not row:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1) from None
    root = state.thread_root(row["id"])
    root_row = state.get_session(root) or row
    explicit = _expectation_from_flags(expect_changes or None, read_only or None)
    inherited = row.get("expect_changes")
    item_expect_changes = (
        bool(inherited)
        if explicit is None and inherited is not None
        else state.expects_changes(prompt, explicit)
    )
    item = queue_mod.enqueue(
        root,
        prompt,
        files=[str(x) for x in (files or [])],
        gate_cmd=gate_cmd if gate_cmd is not None else root_row.get("gate_cmd"),
        model=model if model is not None else row.get("model"),
        expect_changes=item_expect_changes,
        first=first,
        position=position,
    )
    if json_out:
        typer.echo(json.dumps(item))
    else:
        console.print(f"queued #{item['id']} in thread {root[:8]}")


@queue_app.command("edit")
def queue_edit(
    item: int,
    prompt: str | None = typer.Option(None, "--prompt", "-p"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    gate_cmd: str | None = typer.Option(None, "--gate"),
    model: str | None = typer.Option(None, "--model"),
    files: list[Path] | None = typer.Option(None, "--files", "-f"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    state.init_db()
    if prompt is not None or prompt_file is not None:
        prompt = _resolve_prompt(prompt, prompt_file)
    if prompt is None and gate_cmd is None and model is None and files is None:
        err_console.print("[yellow]nothing to change[/yellow]")
        return
    try:
        row = queue_mod.edit(
            item,
            prompt=prompt,
            gate_cmd=gate_cmd,
            model=model,
            files=[str(x) for x in files] if files is not None else None,
        )
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    _queue_event(row, "queue_edited")
    typer.echo(json.dumps(row) if json_out else f"edited #{item}")


@queue_app.command("rm")
def queue_rm(item: int, json_out: bool = typer.Option(False, "--json")) -> None:
    state.init_db()
    try:
        row = queue_mod.cancel(item)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    _queue_event(row, "queue_cancelled")
    typer.echo(json.dumps(row) if json_out else f"cancelled #{item}")


@queue_app.command("clear")
def queue_clear(session_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    state.init_db()
    row = state.get_session(session_id)
    if not row:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    root = state.thread_root(row["id"])
    count = queue_mod.clear(root)
    state.add_event(root, "queue_cancelled", detail={"count": count})
    typer.echo(json.dumps({"cancelled": count}) if json_out else f"cancelled {count} item(s)")


@queue_app.command("move")
def queue_move(
    item: int,
    first: bool = typer.Option(False, "--first"),
    last: bool = typer.Option(False, "--last"),
    to: int | None = typer.Option(None, "--to"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    state.init_db()
    if sum(bool(x) for x in (first, last, to is not None)) != 1:
        raise typer.BadParameter("use one of --first, --last, --to")
    if to is not None and to < 1:
        raise typer.BadParameter("--to must be at least 1")
    row = queue_mod.get(item)
    if not row:
        err_console.print(f"[red]queue item #{item} not found[/red]")
        raise typer.Exit(1)
    position = 1 if first else (queue_mod.queued_count(row["thread_id"]) if last else to or 1)
    try:
        row = queue_mod.move(item, position)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    typer.echo(json.dumps(row) if json_out else f"moved #{item}")


@queue_app.command("drain")
def queue_drain(
    thread: str | None = typer.Option(None, "--thread"),
    force: bool = typer.Option(False, "--force"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    state.init_db()
    try:
        root = state.thread_root(thread) if thread else None
    except ValueError:
        err_console.print(f"[red]session {thread!r} not found[/red]")
        raise typer.Exit(1) from None
    delivered = queue_mod.drain_idle_threads(_spawn_detached, thread_id=root, force=force)
    typer.echo(
        json.dumps({"delivered": delivered}) if json_out else f"delivered {len(delivered)} item(s)"
    )


@app.command("list")
def list_cmd(
    status: str | None = typer.Option(
        None, "--status", help="pending|running|done|failed|killed|no_changes"
    ),
    agent: str | None = typer.Option(None, "--agent", help="filter by agent"),
    json_out: bool = typer.Option(False, "--json"),
    limit: int = typer.Option(50, "--limit"),
    reap: bool = typer.Option(False, "--reap", help="mark dead running/pending sessions killed"),
) -> None:
    """List recent sessions."""
    state.init_db()
    reaped = state.reap_dead_sessions() if reap else []
    if reap:
        typer.echo(f"reaped {len(reaped)} session(s)", err=json_out)
    rows = state.list_sessions(status=status, agent=agent, limit=limit)
    if json_out:
        typer.echo(json.dumps(rows, default=str))
        return
    if not rows:
        console.print("[dim]no sessions[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", style="cyan")
    table.add_column("agent")
    table.add_column("status")
    table.add_column("gate")
    table.add_column("started")
    table.add_column("prompt")
    for r in rows:
        prompt_preview = (r["prompt"] or "").replace("\n", " ")
        if len(prompt_preview) > 60:
            prompt_preview = prompt_preview[:57] + "..."
        table.add_row(
            r["id"][:8],
            r["agent"],
            _status_icon(r["status"]),
            _gate_icon(r.get("gate_status")),
            (r["started_at"] or "")[:19],
            prompt_preview,
        )
    console.print(table)


@app.command("limits")
def limits_(
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable status"),
) -> None:
    """Show running sessions, free RAM and whether a dispatch would be allowed now."""
    state.init_db()
    decision = limits.evaluate()
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "running": len(decision.running),
                    "max_parallel": decision.max_parallel,
                    "free_ram_gb": decision.free_gb,
                    "min_free_ram_gb": decision.min_free_ram_gb,
                    "allowed": decision.allowed,
                    "refusals": decision.refusals,
                }
            )
        )
        return
    free = (
        "unknown (no probe on this platform)"
        if decision.free_gb is None
        else f"{decision.free_gb:.1f} GB"
    )
    console.print(f"running:         {len(decision.running)}")
    console.print(f"max_parallel:    {decision.max_parallel or 'off'}")
    console.print(f"free ram:        {free}")
    console.print(f"min_free_ram_gb: {decision.min_free_ram_gb or 'off'}")
    if decision.allowed:
        console.print("[green]dispatch now:   allowed[/green]")
        return
    console.print("[red]dispatch now:   refused[/red]")
    for refusal in decision.refusals:
        err_console.print(f"[red]{refusal}[/red]")


@worktree_app.command("ls")
def worktree_ls(
    repo: Path | None = typer.Option(None, "--repo", help="repository root (defaults to cwd)"),
    json_out: bool = typer.Option(False, "--json"),
    size: bool = typer.Option(False, "--size", help="include disk usage in KiB"),
) -> None:
    """List worktrees referenced by sessions."""
    state.init_db()
    try:
        root = worktree.repo_root((repo or Path.cwd()).expanduser().resolve())
        rows = _worktree_rows(root)
        registered = {r["path"]: r for r in worktree.list_worktrees(root)}
    except worktree.WorktreeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    items = []
    wt_base = root.parent / f"{root.name}-wt"
    for path_text, sessions in rows.items():
        path = Path(path_text)
        info = registered.get(path_text, {})
        exists = path.is_dir()
        try:
            rel_name = str(path.relative_to(wt_base))
        except ValueError:
            rel_name = path.name
        item = {
            "name": rel_name,
            "path": path_text,
            "branch": info.get("branch", sessions[-1].get("worktree_branch")),
            "exists": exists,
            "dirty": worktree.dirty(path) if exists else False,
            "sessions": len(sessions),
            "last_status": sessions[-1]["status"],
            "age": sessions[0]["started_at"],
        }
        if size and exists:
            item["size_kb"] = _worktree_size(path)
        items.append(item)
    if json_out:
        typer.echo(json.dumps(items))
        return
    table = Table(show_header=True, header_style="bold")
    # Name and branch are never ellipsised, even in a narrow terminal: they
    # keep min_width so the shortened path column takes the squeeze instead.
    # crop=False keeps full logical lines when the table runs wider than the
    # console — Rich would otherwise cut them at the console width.
    name_width = max([len("name"), *(len(item["name"]) for item in items)])
    branch_width = max([len("branch"), *(len(item["branch"] or "") for item in items)])
    table.add_column("name", overflow="fold", min_width=name_width)
    table.add_column("path", overflow="fold")
    table.add_column("branch", overflow="fold", min_width=branch_width)
    for column in ("exists", "dirty", "sessions", "last status", "age"):
        table.add_column(column)
    if size:
        table.add_column("size")
    for item in items:
        values = [
            item["name"],
            f"…/{item['name']}",
            item["branch"] or "",
            "y" if item["exists"] else "n",
            "y" if item["dirty"] else "n",
            str(item["sessions"]),
            item["last_status"],
            item["age"],
        ]
        if size:
            values.append(f"{item.get('size_kb', 0)}K")
        table.add_row(*values)
    console.print(table, crop=False)


@worktree_app.command("rm")
def worktree_rm(
    name_or_path: str = typer.Argument(..., help="worktree name or path"),
    repo: Path | None = typer.Option(None, "--repo"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Remove a session worktree and its branch."""
    state.init_db()
    try:
        root = worktree.repo_root((repo or Path.cwd()).expanduser().resolve())
        path, branch = _resolve_worktree(root, name_or_path)
        kept_reason = _remove_worktree(root, path, branch, force)
        console.print(f"[green]removed[/green] {path}")
        if kept_reason:
            # escape(): git stderr is verbatim text — a "[foo]" in a path is
            # not Rich markup and must not be swallowed as a style tag.
            console.print(f"[yellow]kept branch {escape(branch)}: {escape(kept_reason)}[/yellow]")
    except worktree.WorktreeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _remove_worktree(root: Path, path: Path, branch: str, force: bool) -> str | None:
    """Remove one worktree; return None when the branch was deleted, else the
    reason it was kept (git's stderr, stripped)."""
    sessions = _worktree_rows(root).get(str(path), [])
    if any(s["status"] == "running" for s in sessions):
        raise worktree.WorktreeError("refusing to remove a worktree with running sessions")
    if path.is_dir() and worktree.dirty(path) and not force:
        raise worktree.WorktreeError("refusing to remove a dirty worktree; pass --force")
    remove = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "worktree",
            "remove",
            *(["--force"] if force else []),
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if remove.returncode:
        raise worktree.WorktreeError(
            remove.stderr.strip() or "git worktree remove failed", remove.returncode
        )
    delete = subprocess.run(
        ["git", "-C", str(root), "branch", "-D" if force else "-d", branch],
        text=True,
        capture_output=True,
        check=False,
    )
    if delete.returncode and force:
        raise worktree.WorktreeError(
            delete.stderr.strip() or "git branch delete failed", delete.returncode
        )
    subprocess.run(["git", "-C", str(root), "worktree", "prune"], check=False)
    if delete.returncode:
        # The branch may be checked out in another worktree rather than
        # unmerged — surface git's own reason verbatim instead of guessing.
        return delete.stderr.strip() or "not merged"
    return None


@worktree_app.command("prune-done")
def worktree_prune_done(
    repo: Path | None = typer.Option(None, "--repo"),
    into: str | None = typer.Option(None, "--into"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Remove completed worktrees whose branches are merged."""
    state.init_db()
    try:
        root = worktree.repo_root((repo or Path.cwd()).expanduser().resolve())
        target = (
            into
            or subprocess.run(
                ["git", "-C", str(root), "branch", "--show-current"],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip()
        )
        if not target:
            raise worktree.WorktreeError("cannot prune on a detached HEAD; pass --into REF")
        skipped = False
        for path_text, sessions in _worktree_rows(root).items():
            branch = sessions[-1].get("worktree_branch")
            if not branch or any(s["status"] not in state.TERMINAL_STATUSES for s in sessions):
                continue
            merged = (
                subprocess.run(
                    ["git", "-C", str(root), "merge-base", "--is-ancestor", branch, target],
                    check=False,
                ).returncode
                == 0
            )
            if not merged:
                continue
            if dry_run:
                console.print(path_text)
            else:
                try:
                    path, resolved_branch = _resolve_worktree(root, path_text)
                    kept_reason = _remove_worktree(root, path, resolved_branch, force=False)
                    console.print(f"[green]removed[/green] {path}")
                    if kept_reason:
                        console.print(
                            f"[yellow]kept branch {escape(resolved_branch)}: "
                            f"{escape(kept_reason)}[/yellow]"
                        )
                except worktree.WorktreeError as exc:
                    skipped = True
                    err_console.print(f"[yellow]skipped {path_text}: {exc}[/yellow]")
        if skipped:
            raise typer.Exit(1)
    except worktree.WorktreeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _worktree_rows(root: Path) -> dict[str, list[dict]]:
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE repo_root = ? "
            "AND worktree_path IS NOT NULL ORDER BY started_at ASC",
            (str(root),),
        ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(item["worktree_path"], []).append(item)
    return grouped


def _resolve_worktree(root: Path, name_or_path: str) -> tuple[Path, str]:
    """Map a worktree argument to its registered path and branch.

    An absolute path, or one starting with ./ ../ or ~, resolves as a path;
    anything else is a name under <repo>-wt/ — the same layout
    worktree.prepare creates, so slashed names such as a/b keep working no
    matter the cwd.
    """
    if Path(name_or_path).is_absolute() or name_or_path.startswith(("./", "../", "~")):
        candidate = Path(name_or_path).expanduser().resolve()
    else:
        candidate = root.parent / f"{root.name}-wt" / name_or_path
    for item in worktree.list_worktrees(root):
        if Path(item["path"]).resolve() == candidate.resolve():
            branch = item.get("branch", "").removeprefix("refs/heads/")
            return candidate, branch
    raise worktree.WorktreeError(f"worktree {name_or_path!r} is not registered")


def _worktree_size(path: Path) -> int:
    result = subprocess.run(["du", "-sk", str(path)], text=True, capture_output=True, check=False)
    try:
        return int(result.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


@app.command()
def get(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
    wait: bool = typer.Option(False, "--wait", help="block until session reaches a terminal state"),
    poll_seconds: float = typer.Option(1.0, "--poll", help="polling interval for --wait"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show session metadata + final output."""
    state.init_db()
    row = state.get_session(session_id)
    if row is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    if wait:
        while row["status"] not in state.TERMINAL_STATUSES:
            time.sleep(poll_seconds)
            row = state.get_session(session_id)
            if row is None:
                err_console.print(f"[red]session {session_id!r} vanished while waiting[/red]")
                raise typer.Exit(1)

    output = ""
    if row.get("output_path") and Path(row["output_path"]).exists():
        output = Path(row["output_path"]).read_text(encoding="utf-8")

    if json_out:
        row["output"] = output
        typer.echo(json.dumps(row, default=str))
        return

    console.print(f"[bold]{row['id']}[/bold]  [dim]({row['agent']} · {row['status']})[/dim]")
    console.print(f"  started:  {row['started_at']}")
    if row["finished_at"]:
        console.print(f"  finished: {row['finished_at']}")
    if row.get("fail_reason"):
        console.print(f"  fail:     {row['fail_reason']}")
    if row.get("fallback_of"):
        console.print(f"  fallback of:  {row['fallback_of']}")
    if row.get("fell_back_to"):
        console.print(f"  fell back to: {row['fell_back_to']}")
    if row["cost_usd"] is not None:
        console.print(f"  cost:     ${row['cost_usd']:.4f}")
    if row["session_file_path"]:
        console.print(f"  thread:   {row['session_file_path']}")
    console.print(f"  {_changes_line(row)}")
    if row.get("worktree_path"):
        console.print(f"  worktree: {row['worktree_path']} ({row.get('worktree_branch') or ''})")
    if row.get("gate_status"):
        gate_line = (
            f"  gate:     {row['gate_status']} "
            f"(exit {row.get('gate_exit_code')}; {row.get('gate_log_path')})"
        )
        console.print(gate_line)
    waiting = queue_mod.queued_count(state.thread_root(row["id"]))
    if waiting:
        console.print(f"  queue:    {waiting} item(s) waiting")
    console.print(f"\n[bold]prompt[/bold]\n{row['prompt']}")
    if output:
        console.print("\n[bold]output[/bold]")
        typer.echo(output)


DEFAULT_THREAD_BYTES = 50_000


@app.command()
def events(
    follow: bool = typer.Option(
        False, "--follow", help="keep polling for new events (Ctrl-C to stop)"
    ),
    batch: str | None = typer.Option(None, "--batch", help="filter by batch label"),
    session: str | None = typer.Option(None, "--session", help="filter by session id (prefix ok)"),
    kind: str | None = typer.Option(
        None, "--kind", help="filter by event kind (status|batch|fallback)"
    ),
    since: str | None = typer.Option(
        None, "--since", help="only events at or after this ISO timestamp"
    ),
    last: int | None = typer.Option(
        None,
        "--last",
        help="print only the last N matching events "
        f"(default {DEFAULT_EVENTS_LAST}, or 0 with --follow)",
    ),
    interval: float | None = typer.Option(
        None,
        "--interval",
        help="polling interval for --follow (default [events] interval, 2)",
    ),
) -> None:
    """Print the event stream as JSON lines — one object per line, always.

    The consumer of this command is a program, not a person, so there is no
    table output and no --json flag: every line is a JSON object.
    """
    state.init_db()
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            err_console.print(f"[red]--since must be an ISO timestamp, got {since!r}[/red]")
            raise typer.Exit(2) from None
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
    effective_last = last if last is not None else (0 if follow else DEFAULT_EVENTS_LAST)
    if interval is None:
        interval = _parse_duration(
            config.setting("events", "interval", DEFAULT_EVENTS_INTERVAL),
            DEFAULT_EVENTS_INTERVAL,
        )
    if follow and interval <= 0:
        err_console.print(
            f"[red]--interval must be a positive number of seconds, got {interval}[/red]"
        )
        raise typer.Exit(2)

    filters = dict(batch_id=batch, session_id=session, kind=kind, since=since_dt)

    def _emit(rows: list[dict]) -> None:
        for row in rows:
            # print (not typer.echo) so each line carries its own flush — a
            # piped reader must never wait on a block-buffered stdout.
            print(events_mod.event_to_line(row), flush=True)

    if follow:
        # Snapshot the cursor BEFORE the initial listing and cap that listing
        # at it: rows a racing commit pushes above the cursor belong to the
        # polls, not to --last N, so printed ids never go backwards.
        cursor = events_mod.max_event_id()
        rows = events_mod.list_events(**filters, max_id=cursor, limit=effective_last)
    else:
        rows = events_mod.list_events(**filters, limit=effective_last)
    _emit(rows)
    if not follow:
        return
    try:
        while True:
            time.sleep(interval)
            state.reap_dead_sessions()
            queue_mod.drain_idle_threads(_spawn_detached)
            new_rows = events_mod.list_events(**filters, after_id=cursor)
            _emit(new_rows)
            if new_rows:
                cursor = new_rows[-1]["id"]
    except KeyboardInterrupt:
        return


@app.command()
def thread(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
    last: int = typer.Option(5, "--last", help="show last N turns (ignored with --all)"),
    role: str | None = typer.Option(None, "--role", help="filter to user|assistant|tool"),
    all_: bool = typer.Option(False, "--all", help="emit the entire thread"),
    include_tools: bool = typer.Option(
        False, "--include-tools", help="include tool_calls and tool_results in output"
    ),
    max_bytes: int = typer.Option(
        DEFAULT_THREAD_BYTES,
        "--max-bytes",
        help="hard cap on output bytes; 0 disables. Truncates with explicit warning.",
    ),
    json_out: bool = typer.Option(False, "--json"),
    follow: bool = typer.Option(False, "--follow", help="poll and print new turns until done"),
) -> None:
    """Read a normalized slice of an agent's session file."""
    state.init_db()
    row = state.get_session(session_id)
    if row is None:
        err_console.print(f"[red]session {session_id!r} has no resolvable session_file[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])

    def _parse_turns(current_row: dict, *, require_existing_file: bool) -> list | None:
        session_file_path = current_row.get("session_file_path")
        if not session_file_path:
            return None
        path = Path(session_file_path)
        if require_existing_file and not path.exists():
            return None
        parsed = handler.parse_session_file(path)
        if role:
            parsed = [t for t in parsed if t.role == role]
        return parsed

    def _emit_turns(batch: list, *, limit_bytes: int | None = None) -> None:
        if json_out:
            payload = [
                {
                    "role": t.role,
                    "content": t.content,
                    "tool_calls": t.tool_calls if include_tools else [],
                    "tool_results": t.tool_results if include_tools else [],
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                }
                for t in batch
            ]
            text = json.dumps(payload, indent=2, ensure_ascii=False)
        else:
            text = _render_turns(batch, include_tools=include_tools)
        if limit_bytes is not None:
            text = _maybe_truncate(text, limit_bytes)
        typer.echo(text)

    def _emit_delta(current_row: dict, printed_count: int) -> int:
        current_turns = _parse_turns(current_row, require_existing_file=True)
        if current_turns is None:
            return printed_count
        current_count = len(current_turns)
        if current_count > printed_count:
            _emit_turns(current_turns[printed_count:])
        return max(printed_count, current_count)

    turns = _parse_turns(row, require_existing_file=False)
    if turns is None:
        if not follow:
            err_console.print(f"[red]session {session_id!r} has no resolvable session_file[/red]")
            raise typer.Exit(1)
        printed_count = 0
    else:
        printed_count = len(turns)
        initial_turns = turns
        if not all_:
            initial_turns = initial_turns[-last:] if last > 0 else initial_turns
        _emit_turns(initial_turns, limit_bytes=max_bytes)

    if not follow:
        return

    if row["status"] in state.TERMINAL_STATUSES:
        return

    try:
        while True:
            time.sleep(0.5)
            row = state.get_session(session_id)
            if row is None:
                err_console.print(f"[red]session {session_id!r} vanished while following[/red]")
                raise typer.Exit(1)
            printed_count = _emit_delta(row, printed_count)
            if row["status"] in state.TERMINAL_STATUSES:
                time.sleep(0.5)
                final_row = state.get_session(session_id)
                if final_row is not None:
                    _emit_delta(final_row, printed_count)
                return
    except KeyboardInterrupt:
        err_console.print("[dim]follow stopped[/dim]")
        return


@app.command()
def summary(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show last 2 assistant messages — sugar for `thread --last 2 --role assistant`."""
    state.init_db()
    row = state.get_session(session_id)
    if row is None or not row.get("session_file_path"):
        # fall back to stored output if session_file is missing
        if row and row.get("output_path") and Path(row["output_path"]).exists():
            txt = Path(row["output_path"]).read_text(encoding="utf-8")
            if json_out:
                typer.echo(json.dumps({"output": txt, **_change_json_fields(row)}))
            else:
                console.print(_changes_line(row))
                typer.echo(txt)
            return
        err_console.print(f"[red]session {session_id!r} has no thread to summarize[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])
    turns = [
        t
        for t in handler.parse_session_file(Path(row["session_file_path"]))
        if t.role == "assistant"
    ][-2:]
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "summary": [{"role": t.role, "content": t.content} for t in turns],
                    **_change_json_fields(row),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    console.print(_changes_line(row))
    typer.echo(_render_turns(turns, include_tools=False))


def _render_turns(turns: list, *, include_tools: bool) -> str:
    out: list[str] = []
    for t in turns:
        ts = t.timestamp.strftime("%H:%M:%S") if t.timestamp else "  -  "
        header = f"--- {t.role} @ {ts} ---"
        out.append(header)
        if t.content:
            out.append(t.content)
        if include_tools:
            for tc in t.tool_calls:
                out.append(f"[tool_call] {tc.get('name')}({tc.get('input')})")
            for tr in t.tool_results:
                content = tr.get("content", "")
                if len(content) > 400:
                    content = content[:400] + "...[truncated]"
                out.append(f"[tool_result {tr.get('tool_use_id', '')}] {content}")
        out.append("")
    return "\n".join(out).rstrip()


def _maybe_truncate(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    truncated = raw[:max_bytes].decode("utf-8", errors="ignore")
    return (
        truncated + f"\n\n[!] truncated at {max_bytes} bytes (full size: {len(raw)}). "
        "Use --all or higher --max-bytes to see more."
    )


@app.command()
def logs(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
    follow: bool = typer.Option(False, "--follow", "-f", help="tail -f the log file"),
) -> None:
    """Show subprocess stdout/stderr for a detached session — typically the
    final dispatch output and any spawn-time errors. For live agent progress
    (turns, tool calls), use `playmaker thread <id> --follow` instead."""
    state.init_db()
    row = state.get_session(session_id)
    if row is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    log_path = state.LOGS_DIR / f"{row['id']}.log"
    if not log_path.exists():
        err_console.print(f"[yellow]no log for {row['id']} (was it run with --detach?)[/yellow]")
        raise typer.Exit(1)

    if not follow:
        typer.echo(log_path.read_text(encoding="utf-8", errors="replace"))
        return

    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        # Print existing content first.
        typer.echo(fh.read(), nl=False)
        while True:
            chunk = fh.read()
            if chunk:
                typer.echo(chunk, nl=False)
            row = state.get_session(row["id"])
            if row is None or row["status"] in state.TERMINAL_STATUSES:
                # one final flush
                tail = fh.read()
                if tail:
                    typer.echo(tail, nl=False)
                return
            time.sleep(0.5)


@app.command()
def kill(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
) -> None:
    """SIGTERM a running detached session."""
    state.init_db()
    row = state.get_session(session_id)
    if row is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    if row["status"] not in ("running", "pending"):
        err_console.print(f"[yellow]session is {row['status']}; nothing to kill[/yellow]")
        raise typer.Exit(0)
    pid = row.get("pid")
    if not pid:
        err_console.print(f"[red]no pid recorded for {row['id']}[/red]")
        raise typer.Exit(1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Process already gone; just mark killed.
        pass
    except PermissionError as exc:
        err_console.print(f"[red]cannot kill pid {pid}: {exc}[/red]")
        raise typer.Exit(1) from exc
    state.update_session(row["id"], status="killed", finished_at=state.now_iso(), exit_code=143)
    # killed is terminal too: if this was the batch's last live session, nobody
    # else is left to notice the fan-out drained.
    _maybe_finalize_batch(row.get("batch_id"))
    console.print(f"[magenta]killed[/magenta] {row['id']} (pid {pid})")


@app.command()
def watch() -> None:
    """Live TUI of recent and active sessions. Ctrl-C to exit."""
    state.init_db()
    watch_console = Console()
    try:
        with watcher.Live(
            watcher._build_table(), console=watch_console, refresh_per_second=1
        ) as live:
            while True:
                time.sleep(1)
                queue_mod.drain_idle_threads(_spawn_detached)
                live.update(watcher._build_table())
    except KeyboardInterrupt:
        watch_console.print("[dim]watch stopped[/dim]")


DEFAULT_QUOTAS_MAX_AGE = "5m"


def _parse_duration(value: object, fallback: float) -> float:
    """Compatibility wrapper for the shared duration parser."""
    return state.parse_duration(value, fallback)


def _quotas_age_seconds(path: Path) -> float | None:
    """How old the cached snapshot is, by its own `fetched_at`; None if unreadable."""
    try:
        stamp = json.loads(path.read_text(encoding="utf-8")).get("fetched_at")
        fetched = datetime.fromisoformat(str(stamp))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return (datetime.now(UTC) - fetched).total_seconds()


def _humanize_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{int(seconds / 86400)}d ago"


@app.command()
def quotas(
    refresh: bool = typer.Option(False, "--refresh", help="probe now, whatever the cache says"),
    cached: bool = typer.Option(
        False, "--cached", help="print the stored snapshot without probing, however old it is"
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show current capacity per provider and model.

    The snapshot in ~/.playmaker/quotas.json is refreshed automatically once it is
    older than `[quotas] max_age` (default 5m) — routing decisions made off a stale
    table are worse than no table at all. `--refresh` probes now; `--cached` never
    probes.
    """
    state.init_db()
    max_age = _parse_duration(
        config.setting("quotas", "max_age", DEFAULT_QUOTAS_MAX_AGE),
        _parse_duration(DEFAULT_QUOTAS_MAX_AGE, 300.0),
    )
    age = _quotas_age_seconds(state.QUOTAS_PATH) if state.QUOTAS_PATH.exists() else None
    stale = age is None or age > max_age
    if refresh or (stale and not cached):
        from playmaker import quotas as quotas_mod

        try:
            quotas_mod.refresh_all(state.QUOTAS_PATH)
            age = _quotas_age_seconds(state.QUOTAS_PATH)
        except Exception as exc:
            err_console.print(f"[red]quota refresh failed at the top level:[/red] {exc}")
    if not state.QUOTAS_PATH.exists():
        err_console.print("[yellow]no quotas data yet — try `playmaker quotas --refresh`[/yellow]")
        raise typer.Exit(1)

    text = state.QUOTAS_PATH.read_text(encoding="utf-8")
    if json_out:
        typer.echo(text)
        return

    data = json.loads(text)
    fetched = data.get("fetched_at", "?")
    age = _quotas_age_seconds(state.QUOTAS_PATH)
    if age is None:
        suffix = ""
    elif age > max_age:
        suffix = f"  [yellow]({_humanize_age(age)} — stale, probes did not run)[/yellow]"
    else:
        suffix = f" [dim]({_humanize_age(age)})[/dim]"
    console.print(f"[dim]fetched: {fetched}[/dim]{suffix}")
    for name, info in (data.get("providers") or {}).items():
        console.print()
        _render_provider(name, info)


def _render_provider(name: str, info: dict) -> None:
    def render_windows(windows: list[dict]) -> None:
        # Adaptive name column so longer categorized labels (e.g. "Claude/GPT
        # weekly") keep the bars aligned instead of overflowing a fixed width.
        name_width = max(11, *(len(w["name"]) for w in windows))
        for window in windows:
            pct = window.get("pct_left")
            bar = _bar(pct) if isinstance(pct, int) else " " * 20
            line = f"  [bold]{window['name']:<{name_width}}[/bold] {bar} {pct}% left"
            right_parts = []
            if window.get("reset_relative"):
                right_parts.append(f"resets in {window['reset_relative']}")
            if window.get("forecast"):
                right_parts.append(window["forecast"])
            if window.get("reserve_pct") is not None:
                right_parts.append(f"{window['reserve_pct']}% in reserve")
            if right_parts:
                line += f"   [dim]{'  ·  '.join(right_parts)}[/dim]"
            console.print(line)

    status = info.get("status")
    label_color = {
        "codex": "blue",
        "claude": "magenta",
        "agy": "green",
        "gemini": "cyan",
        "zai": "yellow",
        "kimi": "cyan",
        "muse": "bright_blue",
        "ollama": "bright_cyan",
    }.get(name, "white")
    display = {
        "agy": "Antigravity (agy)",
        "zai": "Z.ai (GLM, via opencode)",
        "kimi": "Kimi Code",
        "muse": "Muse Code",
        "ollama": "Ollama (local, via opencode)",
    }.get(name, name.capitalize())
    title = f"[bold {label_color}]{display}[/bold {label_color}]"
    suffix_parts: list[str] = []
    if info.get("account_email"):
        suffix_parts.append(info["account_email"])
    if info.get("tier"):
        suffix_parts.append(info["tier"])
    # agy has two data sources; "remote" is the coarse Gemini-only fallback.
    if info.get("source") == "remote":
        suffix_parts.append("Gemini-only (daemon offline)")
    suffix = "  ·  ".join(suffix_parts)
    if suffix:
        console.print(f"{title}  [dim]{suffix}[/dim]")
    else:
        console.print(title)

    if status == "error":
        console.print(f"  [red]error[/red]: {info.get('error', '')}")
        if info.get("last_success"):
            console.print(f"  [dim]last success: {info['last_success']}[/dim]")
        return
    if status == "unsupported":
        console.print(f"  [yellow]unsupported[/yellow]: {info.get('reason', '')}")
        return
    # The lane works, only its quota source is out of reach — say how to get it back.
    if status == "unavailable":
        console.print(f"  [yellow]unavailable[/yellow]: {info.get('reason', '')}")
        if info.get("hint"):
            console.print(f"  [dim]hint: {info['hint']}[/dim]")
        if info.get("last_success"):
            console.print(f"  [dim]last success: {info['last_success']}[/dim]")
        return

    windows = info.get("windows") or []
    blocks = info.get("blocks") or []
    note = info.get("note") if status == "ok" else None
    if not windows:
        if note:
            console.print(f"  [dim]{note}[/dim]")
        else:
            console.print("  [dim]no quota windows reported[/dim]")
        if not blocks:
            return
    else:
        render_windows(windows)
        if note:
            console.print(f"  [dim]{note}[/dim]")
    if info.get("source") == "remote" and info.get("local_error"):
        console.print(f"  [dim]local daemon: {info['local_error']}[/dim]")

    # A local provider has models where the metered ones have tiers; naming them
    # saves the coach a round-trip to `ollama list` before it can route here.
    models = info.get("models")
    if models:
        console.print(f"  [dim]models: {', '.join(models)}[/dim]")

    # Metered overage pool ("Extra usage" in Claude's UI) — the bucket that
    # holds usage-credit / Agent-SDK spend. monthly_limit/used arrive in cents.
    extra = info.get("extra_usage")
    if extra and extra.get("monthly_limit_usd") is not None:
        limit = (extra.get("monthly_limit_usd") or 0) / 100
        used = (extra.get("used_credits_usd") or 0) / 100
        util = extra.get("utilization_pct")
        if util is None and limit > 0:
            util = round(used / limit * 100)
        util_str = f"{util}% used" if util is not None else ""
        console.print(
            f"  [bold]{'Extra usage':<11}[/bold] ${used:.2f} / ${limit:.2f}   [dim]{util_str}[/dim]"
        )

    banked_resets = info.get("banked_resets")
    if banked_resets and banked_resets.get("available_count", 0) > 0:
        available = banked_resets["available_count"]
        usable = banked_resets.get("applicable_available_count", 0)
        console.print(f"  [bold]Banked resets[/bold]  {available} ({usable} usable now)")
        expires = banked_resets.get("expires") or []
        if expires:
            console.print(f"  [dim]expire in {' · '.join(expires)}[/dim]")

    for block in blocks:
        block_windows = block.get("windows") or []
        console.print()
        console.print(f"[bold {label_color}]{display} — {block['name']}[/bold {label_color}]")
        if block_windows:
            render_windows(block_windows)


def _bar(pct: int, width: int = 20) -> str:
    pct = max(0, min(100, pct))
    filled = round((pct / 100) * width)
    if pct >= 50:
        color = "green"
    elif pct >= 20:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{'█' * filled}[/{color}]{'░' * (width - filled)}"


@app.command()
def agents() -> None:
    """List registered agents — name, availability, profile path."""
    from playmaker.registry import all_handlers, find_profile

    state.init_db()
    cwd = Path.cwd()
    table = Table(show_header=True, header_style="bold")
    table.add_column("name", style="cyan")
    table.add_column("available")
    table.add_column("profile")
    for name, handler in all_handlers().items():
        avail = "[green]yes[/green]" if handler.is_available() else "[red]no[/red]"
        profile = find_profile(name, cwd)
        profile_str = str(profile) if profile else "[dim]none[/dim]"
        table.add_row(name, avail, profile_str)
    console.print(table)


def _unavailable(agent: str) -> str:
    """Why a dispatch is refused, naming the executable we actually looked for.

    Without the name this reads as "not on PATH" even when the config points
    `binary` at a path that simply does not exist.
    """
    from playmaker.config import agent_binary

    executable = agent_binary(agent)
    hint = "" if executable == agent else f" (agents.{agent}.binary in {state.CONFIG_PATH})"
    return f"[red]agent {agent!r}: {executable!r} not found{hint}[/red]"


def _status_icon(status: str) -> str:
    return {
        "pending": "[yellow]pending[/yellow]",
        "running": "[blue]running[/blue]",
        "done": "[green]done[/green]",
        "no_changes": "[yellow]⚠ no_changes[/yellow]",
        "failed": "[red]failed[/red]",
        "killed": "[magenta]killed[/magenta]",
    }.get(status, status)


def _gate_icon(status: str | None) -> str:
    if status == "pass":
        return "✓"
    if status in {"fail", "timeout"}:
        return "✗"
    return "–"


def _expectation_from_flags(expect_changes: bool | None, read_only: bool | None) -> bool | None:
    if expect_changes and read_only:
        raise typer.BadParameter("--expect-changes and --read-only cannot be used together")
    if expect_changes:
        return True
    if read_only:
        return False
    return None


def _changes_line(row: dict) -> str:
    files_changed = row.get("files_changed")
    if files_changed is None:
        return "changes: unknown"
    noun = "file" if files_changed == 1 else "files"
    suffix = " (⚠ no_changes)" if row.get("status") == "no_changes" else ""
    return f"changes: {files_changed} {noun}{suffix}"


def _change_json_fields(row: dict) -> dict:
    return {
        name: row.get(name)
        for name in ("files_changed", "expect_changes", "pre_tree_hash", "post_tree_hash")
    }


_DEFAULT_CONFIG = """\
# playmaker config
[quotas]
# `playmaker quotas` re-probes automatically once the stored snapshot is older
# than this — a routing decision made off a stale table is worse than no table.
# Probing costs a few seconds; raise it if you call quotas in a tight loop.
max_age = "5m"

[notifications]
on_complete = true
on_fail = true
sound = true
# App that a clicked notification opens the agent's output file in
# (terminal-notifier only). Any app name `open -a` accepts.
editor = "Zed"

# Timeout for the wt-setup hook of `dispatch --worktree`, e.g. "10m" (default "10m").
# [worktree]
# setup_timeout = "10m"

# Timeout for the --gate command after the agent exits (default "20m").
# [gate]
# timeout = "20m"

# Refuse dispatch when running+pending reach N / free RAM below X GB; 0 disables (default 0).
# [limits]
# max_parallel = 0
# min_free_ram_gb = 0

# Poll interval, in seconds, for `events --follow` (default 2.0).
# [events]
# interval = 2.0

# An `unknown` failure within N seconds of start counts as a refusal for --fallback (default 120).
# [fallback]
# window = 120

# How much each sub-agent may do while nobody is watching. A detached agent
# cannot answer a permission prompt, so every agent needs *some* answer here.
#
# `binary` is the executable playmaker launches for that agent. A bare name is
# resolved on PATH; an absolute path (~ allowed) is used as-is. Reach for the
# path form when the CLI installs somewhere only an interactive shell knows
# about — opencode's ~/.opencode/bin is added by a line in .zshrc, so a cron or
# editor-spawned dispatch cannot find it otherwise.

[agents.claude]
binary = "claude"
# "acceptEdits" (default): the agent edits files and runs commands freely
#   inside the dispatch --cwd, and claude itself refuses anything outside it.
# "plan": read and plan only, no writes.
# "bypassPermissions": no boundary at all — same as yolo below.
permission_mode = "acceptEdits"
# Optional allowlist/denylist, narrower than the mode. Tool syntax is Claude
# Code's own, e.g. "Bash(pytest:*)" to permit one command family.
# allowed_tools = ["Read", "Edit", "Write", "Bash(pytest:*)"]
# disallowed_tools = ["WebFetch"]
#
# Escape hatch: skip every check, including the working-directory boundary.
# Only for prompts and directories you would run unattended.
# yolo = true

[agents.codex]
binary = "codex"
# codex exec is already non-interactive and sandboxes the model's shell itself,
# so it needs no permission flag. Set a policy to override its default:
# "read-only" | "workspace-write" | "danger-full-access"
# sandbox = "workspace-write"

[agents.agy]
binary = "agy"
# Antigravity CLI. Model names come from `agy models` verbatim, e.g.
# "claude-opus-4-6-thinking", "gemini-3.6-flash-high". The roster changes
# with Antigravity releases, so read it rather than copying from docs.
#
# agy has no per-mode permission flag — a detached run either auto-approves or
# comes back having done nothing — so this defaults to on.
yolo = true
# Layer agy's own terminal restrictions on top.
# sandbox = true
#
# Forwarded to agy --print-timeout; the CLI's own default (5m) is too short
# for real subtasks.
print_timeout = "60m"

[agents.gemini]
binary = "gemini"

[agents.opencode]
binary = "opencode"
# opencode fronts ~75 providers behind one CLI, so --model is the whole routing
# story. Names are "provider/model" exactly as `opencode models` prints them,
# e.g. "zai-coding-plan/glm-5.2" (GLM), "lmstudio/qwen/qwen3-coder-30b" (local).
#
# Set this. Left unset, opencode falls back to the default in its own
# ~/.config/opencode/opencode.json — whatever you last picked interactively,
# which is often a local model rather than the one you meant to dispatch to.
# model = "zai-coding-plan/glm-5.2"
#
# Like agy, opencode has no per-mode permission flag — only --auto, which
# auto-approves anything not explicitly denied — so a detached run either
# auto-approves or comes back having done nothing. Narrow it with the
# `permission` block in your own opencode.json (edit/bash/webfetch =
# ask|allow|deny), which --auto still respects for denies.
yolo = true
# Forwarded as --agent / --variant when set (variant is provider-specific
# reasoning effort, e.g. "high").
# agent = "build"
# variant = "high"
"""


if __name__ == "__main__":
    app()

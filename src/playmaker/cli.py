"""Typer CLI app — entry point for `playmaker`."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from playmaker import notify, state, watcher
from playmaker.registry import get_handler

app = typer.Typer(
    name="playmaker",
    help="Multi-agent orchestration CLI. Dispatch subtasks to Claude/Codex/Gemini and observe.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


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


@app.command()
def dispatch(
    agent: str = typer.Argument(..., help="agent name (claude|codex|gemini)"),
    prompt: str = typer.Option(..., "--prompt", "-p", help="initial prompt"),
    cwd: Path = typer.Option(
        Path.cwd(),
        "--cwd",
        help="working directory for the agent (defaults to current dir)",
    ),
    files: Optional[list[Path]] = typer.Option(
        None, "--files", "-f", help="files to attach to prompt"
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="forwarded to the agent CLI's --model (e.g. claude 'opus'/'sonnet', "
        "codex 'gpt-5-codex', gemini 'gemini-2.5-pro'); omitted = agent default",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="block until the agent finishes and print final output (default is detached)",
    ),
    parent: Optional[str] = typer.Option(
        None, "--parent", help="parent session id (for delegation tree)"
    ),
    batch: Optional[str] = typer.Option(
        None,
        "--batch",
        help="batch label: pass the same value to every dispatch in one fan-out. "
        "Per-dispatch success pings are suppressed; one summary fires when the "
        "whole batch finishes. Failures still ping immediately.",
    ),
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable result"),
) -> None:
    """Run an agent non-interactively. Detached by default — prints session id
    and returns immediately. Use --sync to block and print the final answer."""
    state.init_db()
    handler = get_handler(agent)
    if not handler.is_available():
        err_console.print(f"[red]agent {agent!r} binary not found on PATH[/red]")
        raise typer.Exit(1)

    cwd_resolved = cwd.expanduser().resolve()
    sid = state.insert_session(
        agent=agent,
        prompt=prompt,
        cwd=str(cwd_resolved),
        files=[str(f) for f in (files or [])],
        parent_id=parent,
        model=model,
        batch_id=batch,
    )

    if sync:
        state.update_session(sid, status="running", pid=os.getpid())
        _run_dispatch(sid)
        return

    log_path = state.LOGS_DIR / f"{sid}.log"
    log_fh = open(log_path, "wb")
    cmd = [sys.executable, "-m", "playmaker", "_run-detached", sid]
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    log_fh.close()
    state.update_session(sid, status="running", pid=proc.pid)
    if json_out:
        typer.echo(json.dumps({"session_id": sid, "pid": proc.pid, "status": "running"}))
    else:
        console.print(f"[dim]session: {sid}  pid: {proc.pid}  (detached)[/dim]")


def _run_dispatch(sid: str) -> None:
    """Execute the dispatch for a pending session and update its row."""
    row = state.get_session(sid)
    if row is None:
        err_console.print(f"[red]session {sid!r} vanished[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])
    cwd = Path(row["cwd"])
    files = [Path(p) for p in json.loads(row["files"] or "[]")]

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
        state.update_session(sid, status="failed", finished_at=state.now_iso(), exit_code=1)
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
        _maybe_finalize_batch(row.get("batch_id"))
        raise typer.Exit(1) from exc

    output_path = _write_output(sid, result.initial_output)

    state.update_session(
        sid,
        status="done",
        finished_at=state.now_iso(),
        agent_session_id=result.agent_session_id,
        session_file_path=str(result.session_file) if result.session_file else None,
        output_path=str(output_path),
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        exit_code=result.exit_code,
    )

    # In a batch, stay quiet per-dispatch — one summary fires when the whole
    # batch drains (see _maybe_finalize_batch). Solo dispatches ping here.
    if not row.get("batch_id"):
        notify.notify(
            f"playmaker — {row['agent']} done",
            _one_line(result.initial_output),
            sound_name="Blow",
            open_path=str(output_path),
            group=f"playmaker-{sid}",
        )
    _maybe_finalize_batch(row.get("batch_id"))

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


def _maybe_finalize_batch(batch_id: Optional[str]) -> None:
    """Fire one summary notification when every session in a batch is terminal.

    Cross-process safe: each detached dispatch calls this on completion; only
    the finisher that wins the O_EXCL sentinel actually notifies.
    """
    if not batch_id:
        return
    siblings = state.list_batch(batch_id)
    if not siblings:
        return
    terminal = {"done", "failed", "killed"}
    if any(s["status"] not in terminal for s in siblings):
        return  # not the last to finish

    sentinel = state.LOGS_DIR / f".batch-{_batch_slug(batch_id)}.done"
    try:
        fd = os.open(str(sentinel), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        return  # another finisher already fired the summary

    ok = [s for s in siblings if s["status"] == "done"]
    marks = " · ".join(f"{s['agent']} {'✓' if s['status'] == 'done' else '✗'}" for s in siblings)
    combined = _render_batch_file(batch_id, siblings)
    notify.notify(
        "playmaker — batch done",
        f"{len(ok)}/{len(siblings)} done · {marks}",
        sound_name="Blow" if len(ok) == len(siblings) else "Basso",
        open_path=str(combined) if combined else None,
        group=f"playmaker-batch-{_batch_slug(batch_id)}",
    )


def _render_batch_file(batch_id: str, siblings: list) -> Optional[Path]:
    """Write a combined markdown view of all batch outputs to /tmp for review."""
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
    target = Path("/tmp") / f"playmaker-batch-{_batch_slug(batch_id)}.md"
    try:
        target.write_text("\n".join(lines), encoding="utf-8")
        return target
    except OSError:
        return None


@app.command("_run-detached", hidden=True)
def _run_detached(session_id: str) -> None:
    """Internal: run a pre-inserted session in the background."""
    state.init_db()
    _run_dispatch(session_id)


@app.command("continue")
def continue_(
    session_id: str = typer.Argument(..., help="existing session id (or unique prefix) to resume"),
    prompt: str = typer.Option(..., "--prompt", "-p", help="follow-up prompt"),
    cwd: Optional[Path] = typer.Option(
        None,
        "--cwd",
        help="override working directory (defaults to the parent session's cwd)",
    ),
    files: Optional[list[Path]] = typer.Option(
        None, "--files", "-f", help="files to attach to prompt"
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="override the model for this turn; defaults to the parent session's model",
    ),
    sync: bool = typer.Option(
        False, "--sync", help="block until done and print final output (default is detached)"
    ),
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable result"),
) -> None:
    """Resume a previous agent session with a follow-up prompt — preserves the
    sub-agent's prior reasoning and tool history. Use this for incremental
    feedback; only fall back to a fresh `dispatch` if context is stale."""
    state.init_db()
    parent = state.get_session(session_id)
    if parent is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    parent_agent_session_id = parent.get("agent_session_id")
    if not parent_agent_session_id:
        err_console.print(
            f"[red]parent session {parent['id'][:8]} has no agent_session_id yet "
            f"(status={parent['status']}); cannot resume[/red]"
        )
        raise typer.Exit(1)

    handler = get_handler(parent["agent"])
    if not handler.is_available():
        err_console.print(f"[red]agent {parent['agent']!r} binary not found on PATH[/red]")
        raise typer.Exit(1)

    cwd_resolved = (cwd or Path(parent["cwd"])).expanduser().resolve()
    effective_model = model if model is not None else parent.get("model")

    # New playmaker session that targets the parent's live agent thread.
    sid = state.insert_session(
        agent=parent["agent"],
        prompt=prompt,
        cwd=str(cwd_resolved),
        files=[str(f) for f in (files or [])],
        parent_id=parent["id"],
        model=effective_model,
    )
    # Pre-populating agent_session_id flips _run_dispatch into resume mode.
    state.update_session(sid, agent_session_id=parent_agent_session_id)

    if sync:
        state.update_session(sid, status="running", pid=os.getpid())
        _run_dispatch(sid)
        return

    log_path = state.LOGS_DIR / f"{sid}.log"
    log_fh = open(log_path, "wb")
    cmd = [sys.executable, "-m", "playmaker", "_run-detached", sid]
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    log_fh.close()
    state.update_session(sid, status="running", pid=proc.pid)
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "session_id": sid,
                    "pid": proc.pid,
                    "status": "running",
                    "resumes": parent_agent_session_id,
                    "parent_id": parent["id"],
                }
            )
        )
    else:
        console.print(
            f"[dim]session: {sid}  pid: {proc.pid}  "
            f"resumes {parent['agent']} thread {parent_agent_session_id[:8]}  (detached)[/dim]"
        )


@app.command("list")
def list_cmd(
    status: Optional[str] = typer.Option(None, "--status", help="pending|running|done|failed|killed"),
    agent: Optional[str] = typer.Option(None, "--agent", help="filter by agent"),
    json_out: bool = typer.Option(False, "--json"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List recent sessions."""
    state.init_db()
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
            (r["started_at"] or "")[:19],
            prompt_preview,
        )
    console.print(table)


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
        terminal = {"done", "failed", "killed"}
        while row["status"] not in terminal:
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
    if row["cost_usd"] is not None:
        console.print(f"  cost:     ${row['cost_usd']:.4f}")
    if row["session_file_path"]:
        console.print(f"  thread:   {row['session_file_path']}")
    console.print(f"\n[bold]prompt[/bold]\n{row['prompt']}")
    if output:
        console.print("\n[bold]output[/bold]")
        typer.echo(output)


DEFAULT_THREAD_BYTES = 50_000


@app.command()
def thread(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
    last: int = typer.Option(5, "--last", help="show last N turns (ignored with --all)"),
    role: Optional[str] = typer.Option(
        None, "--role", help="filter to user|assistant|tool"
    ),
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

    terminal = {"done", "failed", "killed"}
    if row["status"] in terminal:
        return

    try:
        while True:
            time.sleep(0.5)
            row = state.get_session(session_id)
            if row is None:
                err_console.print(f"[red]session {session_id!r} vanished while following[/red]")
                raise typer.Exit(1)
            printed_count = _emit_delta(row, printed_count)
            if row["status"] in terminal:
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
            typer.echo(txt if not json_out else json.dumps({"output": txt}))
            return
        err_console.print(f"[red]session {session_id!r} has no thread to summarize[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])
    turns = [
        t for t in handler.parse_session_file(Path(row["session_file_path"]))
        if t.role == "assistant"
    ][-2:]
    if json_out:
        typer.echo(
            json.dumps(
                [{"role": t.role, "content": t.content} for t in turns],
                indent=2,
                ensure_ascii=False,
            )
        )
        return
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
        truncated
        + f"\n\n[!] truncated at {max_bytes} bytes (full size: {len(raw)}). "
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
        terminal = {"done", "failed", "killed"}
        while True:
            chunk = fh.read()
            if chunk:
                typer.echo(chunk, nl=False)
            row = state.get_session(row["id"])
            if row is None or row["status"] in terminal:
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
        raise typer.Exit(1)
    state.update_session(
        row["id"], status="killed", finished_at=state.now_iso(), exit_code=143
    )
    console.print(f"[magenta]killed[/magenta] {row['id']} (pid {pid})")


@app.command()
def watch() -> None:
    """Live TUI of recent and active sessions. Ctrl-C to exit."""
    watcher.run()


@app.command()
def quotas(
    refresh: bool = typer.Option(False, "--refresh", help="re-run probes before printing"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show ~/.playmaker/quotas.json. With --refresh, run probes first."""
    state.init_db()
    if refresh or not state.QUOTAS_PATH.exists():
        from playmaker import quotas as quotas_mod

        try:
            quotas_mod.refresh_all(state.QUOTAS_PATH)
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
    console.print(f"[dim]fetched: {fetched}[/dim]")
    for name, info in (data.get("providers") or {}).items():
        console.print()
        _render_provider(name, info)


def _render_provider(name: str, info: dict) -> None:
    status = info.get("status")
    label_color = {"codex": "blue", "claude": "magenta", "gemini": "cyan"}.get(name, "white")
    title = f"[bold {label_color}]{name.capitalize()}[/bold {label_color}]"
    suffix_parts: list[str] = []
    if info.get("account_email"):
        suffix_parts.append(info["account_email"])
    if info.get("tier"):
        suffix_parts.append(info["tier"])
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

    windows = info.get("windows") or []
    if not windows:
        console.print("  [dim]no quota windows reported[/dim]")
        return

    for w in windows:
        pct = w.get("pct_left")
        bar = _bar(pct) if isinstance(pct, int) else " " * 20
        line = f"  [bold]{w['name']:<11}[/bold] {bar} {pct}% left"
        right_parts = []
        if w.get("reset_relative"):
            right_parts.append(f"resets in {w['reset_relative']}")
        if w.get("forecast"):
            right_parts.append(w["forecast"])
        if w.get("reserve_pct") is not None:
            right_parts.append(f"{w['reserve_pct']}% in reserve")
        if right_parts:
            line += f"   [dim]{'  ·  '.join(right_parts)}[/dim]"
        console.print(line)

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
            f"  [bold]{'Extra usage':<11}[/bold] ${used:.2f} / ${limit:.2f}"
            f"   [dim]{util_str}[/dim]"
        )


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


def _status_icon(status: str) -> str:
    return {
        "pending": "[yellow]pending[/yellow]",
        "running": "[blue]running[/blue]",
        "done": "[green]done[/green]",
        "failed": "[red]failed[/red]",
        "killed": "[magenta]killed[/magenta]",
    }.get(status, status)


_DEFAULT_CONFIG = """\
# playmaker config
[notifications]
on_complete = true
on_fail = true
sound = true

[agents.claude]
binary = "claude"
# Headless dispatch passes --dangerously-skip-permissions so unattended runs
# are not blocked by tool-permission prompts. Set to false to keep Claude
# Code's normal permission checks (detached runs will then stall on the first
# tool prompt and finish without writing anything).
skip_permissions = true

[agents.codex]
binary = "codex"

[agents.gemini]
binary = "gemini"
"""


if __name__ == "__main__":
    app()

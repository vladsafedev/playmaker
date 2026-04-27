"""Typer CLI app — entry point for `team`."""

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

from team import notify, state, watcher, zed
from team.registry import get_handler

app = typer.Typer(
    name="team",
    help="Multi-agent orchestration CLI. Dispatch subtasks to Claude/Codex/Gemini and observe.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


@app.command()
def init() -> None:
    """Bootstrap ~/.team/ structure (config, state.db, dirs)."""
    state.init_db()
    if not state.CONFIG_PATH.exists():
        state.CONFIG_PATH.write_text(_DEFAULT_CONFIG, encoding="utf-8")
    console.print(f"[green]initialized[/green] {state.TEAM_HOME}")
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
    sync: bool = typer.Option(
        False,
        "--sync",
        help="block until the agent finishes and print final output (default is detached)",
    ),
    parent: Optional[str] = typer.Option(
        None, "--parent", help="parent session id (for delegation tree)"
    ),
    register_zed: bool = typer.Option(
        True,
        "--register-zed/--no-register-zed",
        help="upsert into Zed's sidebar_threads on completion (default on; non-interactive runs are filtered out by Zed's native Import)",
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
    )

    if sync:
        state.update_session(sid, status="running", pid=os.getpid())
        _run_dispatch(sid, register_zed=register_zed)
        return

    log_path = state.LOGS_DIR / f"{sid}.log"
    log_fh = open(log_path, "wb")
    cmd = [sys.executable, "-m", "team", "_run-detached", sid]
    env = os.environ.copy()
    if not register_zed:
        env["TEAM_NO_REGISTER_ZED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    log_fh.close()
    state.update_session(sid, status="running", pid=proc.pid)
    if json_out:
        typer.echo(json.dumps({"session_id": sid, "pid": proc.pid, "status": "running"}))
    else:
        console.print(f"[dim]session: {sid}  pid: {proc.pid}  (detached)[/dim]")


def _run_dispatch(sid: str, *, register_zed: bool = True) -> None:
    """Execute the dispatch for a pending session and update its row."""
    row = state.get_session(sid)
    if row is None:
        err_console.print(f"[red]session {sid!r} vanished[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])
    cwd = Path(row["cwd"])
    files = [Path(p) for p in json.loads(row["files"] or "[]")]

    try:
        result = handler.dispatch(row["prompt"], cwd, files)
    except Exception as exc:
        state.update_session(sid, status="failed", finished_at=state.now_iso(), exit_code=1)
        err_console.print(f"[red]dispatch failed:[/red] {exc}")
        notify.notify("team — dispatch failed", f"{row['agent']}: {exc}", sound=True)
        raise typer.Exit(1) from exc

    output_path = state.OUTPUTS_DIR / f"{sid}.txt"
    output_path.write_text(result.initial_output, encoding="utf-8")

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

    if register_zed:
        try:
            zed.register(
                agent=row["agent"],
                agent_session_id=result.agent_session_id,
                prompt=row["prompt"],
                cwd=row["cwd"],
                started_at_iso=row["started_at"],
            )
        except Exception as exc:
            # Best-effort: registration failure shouldn't fail the dispatch.
            err_console.print(f"[yellow]zed register skipped:[/yellow] {exc}")
        # register() returns None when Zed already has the row (e.g. Claude
        # auto-imported by Zed); that's fine, no message needed.

    notify.notify(
        "team — done",
        f"{row['agent']}: {result.initial_output[:80]}",
        sound=True,
    )

    console.print(f"[dim]session: {sid}  agent_session: {result.agent_session_id}[/dim]")
    typer.echo(result.initial_output)


@app.command("_run-detached", hidden=True)
def _run_detached(session_id: str) -> None:
    """Internal: run a pre-inserted session in the background."""
    state.init_db()
    register = os.environ.get("TEAM_NO_REGISTER_ZED") != "1"
    _run_dispatch(session_id, register_zed=register)


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
) -> None:
    """Read a normalized slice of an agent's session file."""
    state.init_db()
    row = state.get_session(session_id)
    if row is None or not row.get("session_file_path"):
        err_console.print(f"[red]session {session_id!r} has no resolvable session_file[/red]")
        raise typer.Exit(1)
    handler = get_handler(row["agent"])
    turns = handler.parse_session_file(Path(row["session_file_path"]))
    if role:
        turns = [t for t in turns if t.role == role]
    if not all_:
        turns = turns[-last:] if last > 0 else turns

    if json_out:
        payload = [
            {
                "role": t.role,
                "content": t.content,
                "tool_calls": t.tool_calls if include_tools else [],
                "tool_results": t.tool_results if include_tools else [],
                "timestamp": t.timestamp.isoformat() if t.timestamp else None,
            }
            for t in turns
        ]
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        text = _maybe_truncate(text, max_bytes)
        typer.echo(text)
        return

    rendered = _render_turns(turns, include_tools=include_tools)
    rendered = _maybe_truncate(rendered, max_bytes)
    typer.echo(rendered)


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
    """Show subprocess stdout/stderr for a detached session."""
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
    """Show ~/.team/quotas.json. With --refresh, run probes first."""
    state.init_db()
    if refresh or not state.QUOTAS_PATH.exists():
        from team import quotas as quotas_mod

        try:
            quotas_mod.refresh_all(state.QUOTAS_PATH)
        except Exception as exc:
            err_console.print(f"[red]quota refresh failed at the top level:[/red] {exc}")
    if not state.QUOTAS_PATH.exists():
        err_console.print("[yellow]no quotas data yet — try `team quotas --refresh`[/yellow]")
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


@app.command("register-zed")
def register_zed_cmd(
    session_id: str = typer.Argument(..., help="session id or unique prefix"),
) -> None:
    """Backfill an existing session into Zed's sidebar_threads.

    Useful for sessions that were dispatched with --no-register-zed, or those
    spawned before the register-zed default was introduced. Restart Zed to see
    the entry.
    """
    state.init_db()
    row = state.get_session(session_id)
    if row is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
        raise typer.Exit(1)
    if not row.get("agent_session_id"):
        err_console.print(
            f"[red]session has no agent_session_id yet (status={row['status']})[/red]"
        )
        raise typer.Exit(1)
    try:
        thread_id = zed.register(
            agent=row["agent"],
            agent_session_id=row["agent_session_id"],
            prompt=row["prompt"],
            cwd=row["cwd"],
            started_at_iso=row["started_at"],
        )
    except Exception as exc:
        err_console.print(f"[red]register failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    if thread_id is None:
        console.print(
            f"[dim]{row['id'][:8]}  already in Zed (Zed Import handled it); skipping[/dim]"
        )
    else:
        console.print(
            f"[green]registered[/green] {row['id'][:8]}  "
            f"thread_id={thread_id.hex()[:8]}  "
            f"[dim](restart Zed to see)[/dim]"
        )


@app.command()
def agents() -> None:
    """List registered agents — name, availability, profile path."""
    from team.registry import all_handlers, find_profile

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
# team config
[notifications]
on_complete = true
on_fail = true
sound = true

[zed]
# We rely on Zed's native "Import External Agent Threads" — no INSERT.
# This block is reserved for future tweaks.

[agents.claude]
binary = "claude"

[agents.codex]
binary = "codex"

[agents.gemini]
binary = "gemini"
"""


if __name__ == "__main__":
    app()

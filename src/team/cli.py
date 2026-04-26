"""Typer CLI app — entry point for `team`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from team import state
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
    detach: bool = typer.Option(
        False, "--detach", help="run in background, return session id immediately (Phase 4)"
    ),
    parent: Optional[str] = typer.Option(
        None, "--parent", help="parent session id (for delegation tree)"
    ),
    json_out: bool = typer.Option(False, "--json", help="emit machine-readable result"),
) -> None:
    """Run an agent non-interactively. Sync mode prints the final assistant text."""
    if detach:
        err_console.print("[yellow]--detach not implemented yet (Phase 4)[/yellow]")
        raise typer.Exit(2)

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
    state.update_session(sid, status="running")

    try:
        result = handler.dispatch(prompt, cwd_resolved, files or [])
    except Exception as exc:
        state.update_session(sid, status="failed", finished_at=state.now_iso(), exit_code=1)
        err_console.print(f"[red]dispatch failed:[/red] {exc}")
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

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "session_id": sid,
                    "agent_session_id": result.agent_session_id,
                    "session_file": str(result.session_file) if result.session_file else None,
                    "output": result.initial_output,
                    "cost_usd": result.cost_usd,
                    "duration_seconds": result.duration_seconds,
                }
            )
        )
    else:
        console.print(f"[dim]session: {sid}  agent_session: {result.agent_session_id}[/dim]")
        typer.echo(result.initial_output)


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
    wait: bool = typer.Option(False, "--wait", help="block until done (Phase 4)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show session metadata + final output."""
    state.init_db()
    if wait:
        err_console.print("[yellow]--wait not implemented yet (Phase 4)[/yellow]")
    row = state.get_session(session_id)
    if row is None:
        err_console.print(f"[red]session {session_id!r} not found[/red]")
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

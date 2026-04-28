"""`playmaker acp` Typer subcommand: register playmaker as Zed agent_server."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import typer

from playmaker.acp.proxy import CHILD_CMD_BY_AGENT, run_proxy


acp_app = typer.Typer(
    name="acp",
    help="Run the ACP middleware proxy on stdio. Register in Zed's agent_servers.",
    no_args_is_help=False,
    add_completion=False,
)


@acp_app.callback(invoke_without_command=True)
def acp(
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        help=(
            "Which sub-agent this proxy instance fronts. One of "
            "claude/codex/gemini. Selects the child command Zed-side "
            "spawns on session/new (Plus-menu path). Defaults to claude. "
            "Ignored when --child is given."
        ),
    ),
    child_cmd: Optional[str] = typer.Option(
        None,
        "--child",
        help=(
            "Explicit child agent command (space-separated). Overrides "
            "--agent. Use this for testing against a custom binary."
        ),
    ),
    log_level: str = typer.Option(
        "INFO", "--log-level", help="Logger level (DEBUG/INFO/WARNING/ERROR)"
    ),
) -> None:
    """Run as ACP server on stdio."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if child_cmd:
        cmd: Optional[list[str]] = child_cmd.split()
    elif agent:
        if agent not in CHILD_CMD_BY_AGENT:
            typer.echo(
                f"unknown --agent {agent!r}; expected one of "
                f"{', '.join(sorted(CHILD_CMD_BY_AGENT))}",
                err=True,
            )
            raise typer.Exit(2)
        cmd = list(CHILD_CMD_BY_AGENT[agent])
    else:
        cmd = None  # falls back to module-level DEFAULT_CHILD_CMD (claude)

    try:
        rc = asyncio.run(run_proxy(child_cmd=cmd))
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else 1
    raise typer.Exit(rc)

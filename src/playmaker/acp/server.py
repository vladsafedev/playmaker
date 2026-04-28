"""`playmaker acp` Typer subcommand: register playmaker as Zed agent_server."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import typer

from playmaker.acp.proxy import run_proxy


acp_app = typer.Typer(
    name="acp",
    help="Run the ACP middleware proxy on stdio. Register in Zed's agent_servers.",
    no_args_is_help=False,
    add_completion=False,
)


@acp_app.callback(invoke_without_command=True)
def acp(
    child_cmd: Optional[str] = typer.Option(
        None,
        "--child",
        help=(
            "Override the child agent command (space-separated). "
            "Defaults to claude-agent-acp via npm exec."
        ),
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", help="Logger level (DEBUG/INFO/WARNING/ERROR)"
    ),
) -> None:
    """Run as ACP server on stdio."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cmd = child_cmd.split() if child_cmd else None
    try:
        rc = asyncio.run(run_proxy(child_cmd=cmd))
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else 1
    raise typer.Exit(rc)

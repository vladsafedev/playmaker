"""Typer CLI app — entry point for `team`."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="team",
    help="Multi-agent orchestration CLI. Dispatch subtasks to Claude/Codex/Gemini and observe.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def init() -> None:
    """Bootstrap ~/.team/ structure (config, state.db, dirs)."""
    typer.echo("init: not yet implemented")


if __name__ == "__main__":
    app()

"""Rich Live TUI for `team watch`."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.table import Table

from team import state


_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "failed": "❌",
    "killed": "🛑",
}


def _build_table() -> Table:
    rows = state.list_sessions(limit=50)
    # Keep non-terminal + recently terminated (last 5 minutes).
    now = datetime.now(timezone.utc)
    keep = []
    for r in rows:
        if r["status"] in ("pending", "running"):
            keep.append(r)
        elif r["finished_at"]:
            try:
                finished = datetime.fromisoformat(r["finished_at"])
            except ValueError:
                continue
            if (now - finished).total_seconds() < 300:
                keep.append(r)

    table = Table(
        title=f"team — live sessions ({len(keep)})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("agent", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("age", no_wrap=True)
    table.add_column("prompt")

    for r in keep:
        try:
            started = datetime.fromisoformat(r["started_at"])
            age_s = (now - started).total_seconds()
        except (TypeError, ValueError):
            age_s = 0
        age_str = _format_age(age_s)
        icon = _ICONS.get(r["status"], "?")
        prompt = (r["prompt"] or "").replace("\n", " ")
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        table.add_row(r["id"][:8], r["agent"], f"{icon} {r['status']}", age_str, prompt)
    return table


def _format_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    h, rem = divmod(seconds, 3600)
    return f"{h}h{rem // 60:02d}m"


def run() -> None:
    state.init_db()
    console = Console()
    try:
        with Live(_build_table(), console=console, refresh_per_second=1) as live:
            while True:
                time.sleep(1)
                live.update(_build_table())
    except KeyboardInterrupt:
        console.print("[dim]watch stopped[/dim]")

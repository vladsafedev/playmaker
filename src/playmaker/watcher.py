"""Rich Live TUI for `playmaker watch`."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.table import Table

from playmaker import state
from playmaker.registry import get_handler

_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "no_changes": "⚠",
    "failed": "❌",
    "killed": "🛑",
}


def _build_table() -> Table:
    rows = state.list_sessions(limit=50)
    # Keep non-terminal + recently terminated (last 5 minutes).
    now = datetime.now(UTC)
    keep = []
    for r in rows:
        if r["status"] not in state.TERMINAL_STATUSES:
            keep.append(r)
        elif r["finished_at"]:
            try:
                finished = datetime.fromisoformat(r["finished_at"])
            except ValueError:
                continue
            if (now - finished).total_seconds() < 300:
                keep.append(r)

    table = Table(
        title=f"playmaker — live sessions ({len(keep)})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("agent", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("age", no_wrap=True)
    table.add_column("activity")
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
        activity = _get_activity(r)
        table.add_row(
            r["id"][:8],
            r["agent"],
            f"{icon} {r['status']}",
            age_str,
            activity,
            prompt,
        )
    return table


def _get_activity(row: dict[str, Any]) -> str:
    status = row["status"]
    if status == "running":
        path_str = row.get("session_file_path")
        if not path_str:
            return "starting..."
        path = Path(path_str)
        if not path.exists():
            return "starting..."
        try:
            handler = get_handler(row["agent"])
            turns = handler.parse_session_file(path)
            if not turns:
                return "starting..."
            last = turns[-1]
            if last.tool_calls:
                # show "tool: <name>" of the LAST tool_call's 'name' field
                name = last.tool_calls[-1].get("name", "")
                text = f"tool: {name}"
            elif last.content:
                text = last.content
            else:
                return "starting..."

            text = text.replace("\n", " ")
            if len(text) > 60:
                text = text[:57] + "..."
            return text
        except Exception:
            return "-"

    if status in ("done", "no_changes"):
        out_path_str = row.get("output_path")
        if not out_path_str:
            return "-"
        path = Path(out_path_str)
        if not path.exists():
            return "-"
        try:
            text = path.read_text().replace("\n", " ")
            if len(text) > 60:
                text = text[:57] + "..."
            return text
        except Exception:
            return "-"

    return "-"


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

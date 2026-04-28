"""Best-effort macOS notifications via osascript."""

from __future__ import annotations

import shlex
import subprocess


def notify(title: str, message: str, *, sound: bool = True) -> None:
    """Fire a macOS notification. Silent failure (logging only) if osascript missing."""
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    sound_clause = ' sound name "Submarine"' if sound else ""
    script = (
        f'display notification "{safe_msg}" with title "{safe_title}"{sound_clause}'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def shell_quote(value: str) -> str:
    return shlex.quote(value)

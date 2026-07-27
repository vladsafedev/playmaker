"""macOS notifications.

Prefers `terminal-notifier` (clickable — opens a file in the editor on click;
distinct sounds) and falls back to `osascript` (no click) when it is absent.
Silent best-effort: never raises into the dispatch path.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess

from playmaker.config import setting

# Fallback app used to open agent output when a notification is clicked;
# override with `editor = "..."` under [notifications] in config.toml.
DEFAULT_OPEN_WITH_APP = "Zed"


def open_with_app() -> str:
    return str(setting("notifications", "editor", DEFAULT_OPEN_WITH_APP))


def notify(
    title: str,
    message: str,
    *,
    sound: bool = True,
    sound_name: str = "Blow",
    open_path: str | None = None,
    group: str | None = None,
) -> None:
    """Fire a macOS notification.

    `open_path` — file to open in the configured editor when the banner is
    clicked (terminal-notifier only). `group` — coalesce key; same group
    replaces.
    """
    if shutil.which("terminal-notifier"):
        _terminal_notifier(title, message, sound, sound_name, open_path, group)
    else:
        _osascript(title, message, sound, sound_name)


def _terminal_notifier(
    title: str,
    message: str,
    sound: bool,
    sound_name: str,
    open_path: str | None,
    group: str | None,
) -> None:
    args = ["terminal-notifier", "-title", title, "-message", message]
    if sound:
        args += ["-sound", sound_name]
    if group:
        args += ["-group", group]
    if open_path:
        # Click → open the file in the editor. Absolute `open` path: -execute
        # runs under a minimal PATH.
        cmd = f"/usr/bin/open -a {shlex.quote(open_with_app())} {shlex.quote(open_path)}"
        args += ["-execute", cmd]
    try:
        subprocess.run(args, capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _osascript(title: str, message: str, sound: bool, sound_name: str) -> None:
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    sound_clause = f' sound name "{sound_name}"' if sound else ""
    script = f'display notification "{safe_msg}" with title "{safe_title}"{sound_clause}'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def shell_quote(value: str) -> str:
    return shlex.quote(value)

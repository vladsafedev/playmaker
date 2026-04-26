"""Quota probes for the supported agents.

Strategy chosen in Phase 5.0 of the project plan:

- ClaudeProbe → spawn `claude /usage` in a PTY, ANSI-strip, regex.
  Stable enough for v1; rotates with Claude Code's UX. We extract just the
  percentages and the raw 'Resets ...' phrase rather than guessing structured
  fields that may rename.
- GeminiProbe → stub returning {status: 'unsupported'}. Reverse-engineering
  Code Assist `loadCodeAssist` / `retrieveUserQuota` (CodexBar's approach) is
  more than v1 can absorb. Slated for v1.1.
- CodexProbe → stub returning {status: 'unsupported'}. Codex's quota lives
  behind authenticated browser cookies + WebKit React internals
  (CodexBar's approach); not portable to Python in a day.

Each probe is wrapped in try/except by the caller, so a single broken probe
cannot bring down `team quotas`.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import time
from datetime import datetime, timezone
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
BOX_RE = re.compile(r"[│┃┏┓┗┛┌┐└┘─━╭╮╯╰▏▕▎▍▌▋▊▉█▐▌▘▝▖▗]")
PERCENT_RE = re.compile(r"(\d+)\s*%\s*used", re.IGNORECASE)
RESET_RE = re.compile(r"Resets?[^A-Za-z]*([A-Za-z]+\s*\d+\s*at\s*\d{1,2}(?::\d{2})?\s*(?:am|pm).*?\))", re.IGNORECASE)


# ---- ClaudeProbe (real) -----------------------------------------------------


def _spawn_pty_capture(cmd: list[str], *, timeout_s: float = 10.0, quiet_after: float = 1.5) -> str:
    """Run `cmd` under a PTY, return decoded output. Stops after `quiet_after`
    seconds with no new bytes once any '%' has been observed, or at `timeout_s`."""
    pid, fd = pty.fork()
    if pid == 0:
        # Child
        os.execvp(cmd[0], cmd)
    out = b""
    deadline = time.time() + timeout_s
    last_change = time.time()
    last_size = 0
    while time.time() < deadline:
        rlist, _, _ = select.select([fd], [], [], 0.2)
        if rlist:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if len(out) != last_size:
                last_change = time.time()
                last_size = len(out)
        if b"%" in out and (time.time() - last_change) > quiet_after:
            break

    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    text = out.decode("utf-8", errors="replace")
    text = ANSI_RE.sub("", text)
    text = BOX_RE.sub(" ", text)
    text = re.sub(r" {3,}", "  ", text)
    return text


def claude_probe() -> dict:
    """Run `claude /usage` in a PTY and parse session/weekly percentages."""
    text = _spawn_pty_capture(["claude", "/usage"])

    percents = [int(m.group(1)) for m in PERCENT_RE.finditer(text)]
    resets = [m.group(1) for m in RESET_RE.finditer(text)]

    if not percents:
        # Either unauthenticated or UX changed underneath us.
        snippet = text.strip()[-300:]
        raise RuntimeError(f"no '% used' found in /usage output; tail: {snippet!r}")

    session_used = percents[0] if len(percents) >= 1 else None
    week_all_used = percents[1] if len(percents) >= 2 else None
    week_sonnet_used = percents[2] if len(percents) >= 3 else None

    return {
        "status": "ok",
        "session_left": _left(session_used),
        "weekly_all_left": _left(week_all_used),
        "weekly_sonnet_left": _left(week_sonnet_used),
        "session_resets": resets[0] if len(resets) >= 1 else None,
        "weekly_resets": resets[1] if len(resets) >= 2 else None,
        "raw_used_pcts": percents[:3],
    }


def _left(used: int | None) -> int | None:
    if used is None:
        return None
    return max(0, 100 - used)


# ---- Stubs ------------------------------------------------------------------


def gemini_probe() -> dict:
    return {
        "status": "unsupported",
        "reason": "Code Assist API (loadCodeAssist + retrieveUserQuota) "
        "uses undocumented endpoints; deferred to v1.1.",
    }


def codex_probe() -> dict:
    return {
        "status": "unsupported",
        "reason": "Codex quota lives behind WebKit-scraped chatgpt.com dashboard; "
        "not portable to Python in v1.",
    }


# ---- Aggregator -------------------------------------------------------------


PROBES = {
    "claude": claude_probe,
    "gemini": gemini_probe,
    "codex": codex_probe,
}


def refresh_all(quotas_path: Path) -> dict:
    """Run every probe; collect results (or per-provider error). Atomic write."""
    previous: dict = {}
    if quotas_path.exists():
        try:
            previous = json.loads(quotas_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}

    out: dict = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "providers": {},
    }
    for name, probe in PROBES.items():
        last_success = (
            previous.get("providers", {}).get(name, {}).get("last_success")
        )
        try:
            result = probe()
            if result.get("status") == "ok":
                result["last_success"] = out["fetched_at"]
            else:
                result["last_success"] = last_success
            out["providers"][name] = result
        except Exception as exc:
            out["providers"][name] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "last_success": last_success,
            }

    tmp = quotas_path.with_suffix(quotas_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(quotas_path)
    return out

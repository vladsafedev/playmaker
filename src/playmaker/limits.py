"""[limits] guard: running-session count and free RAM as a dispatch decision.

`dispatch` and an immediate `continue` consult this before inserting a session
row: two simultaneous fan-outs can put a 36 GB Mac down, so the CLI refuses to
exceed the parallelism and headroom the operator configured. Both checks are
off by default (`max_parallel = 0`, `min_free_ram_gb = 0`).
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playmaker import config, state

FORCE_HINT = "pass --force to dispatch anyway"


def _int_setting(key: str, default: int) -> int:
    try:
        return int(config.setting("limits", key, default))
    except (TypeError, ValueError):
        return default


def _float_setting(key: str, default: float) -> float:
    try:
        return float(config.setting("limits", key, default))
    except (TypeError, ValueError):
        return default


def running_sessions() -> list[dict]:
    """Sessions that occupy a lane right now: running or pending, oldest first."""
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status IN ('running', 'pending') "
            "ORDER BY started_at ASC"
        ).fetchall()
    return [dict(row) for row in rows if state.session_is_alive(row)]


def _system() -> str:
    return platform.system()


def _free_gb_darwin() -> float | None:
    # A probe that fails — for any reason — means "unknown", and an unknown
    # reading must skip the check, never refuse or crash a dispatch.
    try:
        proc = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    page_size = None
    pages: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        head, sep, rest = line.partition(":")
        if not sep:
            continue
        if head.strip() == "Mach Virtual Memory Statistics":
            if "page size of" not in rest:
                return None
            try:
                page_size = int(rest.split("page size of", 1)[1].split("bytes", 1)[0].strip())
            except ValueError:
                return None
            continue
        key = head.strip().removeprefix("Pages ").lower()
        if key in {"free", "inactive", "speculative"}:
            try:
                pages[key] = int(rest.strip().rstrip("."))
            except ValueError:
                continue
    if page_size is None or not pages:
        return None
    return page_size * sum(pages.values()) / 1024**3


def _free_gb_linux() -> float | None:
    # Same contract as the darwin probe: any failure reads as "unknown".
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                kb = int(line.split()[1])
            except (IndexError, ValueError):
                return None
            return kb * 1024 / 1024**3
    return None


def free_ram_gb() -> float | None:
    """Free RAM in GB, or None when the platform has no known probe.

    darwin: vm_stat pages free + inactive + speculative, times the page size.
    linux: /proc/meminfo MemAvailable. Anything else: the check is skipped.
    """
    system = _system()
    if system == "Darwin":
        return _free_gb_darwin()
    if system == "Linux":
        return _free_gb_linux()
    return None


def _age(started_at: str) -> str:
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return "?"
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - started).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{int(seconds / 86400)}d ago"


@dataclass
class Decision:
    """One look at the world through the [limits] config."""

    running: list[dict]
    max_parallel: int
    free_gb: float | None
    min_free_ram_gb: float
    refusals: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.refusals


def evaluate() -> Decision:
    """Count running sessions and free RAM; refuse when a configured limit is exceeded."""
    max_parallel = _int_setting("max_parallel", 0)
    min_free = _float_setting("min_free_ram_gb", 0.0)
    running = running_sessions()
    refusals: list[str] = []
    if max_parallel > 0 and len(running) >= max_parallel:
        listing = "\n".join(
            f"  {row['id'][:8]}  {row['agent']}  started {_age(row['started_at'])}"
            for row in running
        )
        refusals.append(
            f"max_parallel={max_parallel} reached: {len(running)} session(s) running or pending\n"
            f"{listing}\n{FORCE_HINT}"
        )
    free = free_ram_gb()
    if min_free > 0 and free is not None and free < min_free:
        refusals.append(
            f"free RAM {free:.1f} GB < min_free_ram_gb={min_free} GB\n{FORCE_HINT}"
        )
    return Decision(
        running=running,
        max_parallel=max_parallel,
        free_gb=free,
        min_free_ram_gb=min_free,
        refusals=refusals,
    )

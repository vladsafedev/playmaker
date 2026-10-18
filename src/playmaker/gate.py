"""Acceptance-gate execution for completed sessions."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateResult:
    status: str
    exit_code: int
    log_path: Path


def run(command: str, cwd: Path, log_path: Path, timeout: float) -> GateResult:
    """Run a shell gate, appending combined output to its dedicated log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        try:
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return GateResult("timeout", 124, log_path)
    return GateResult(
        "pass" if completed.returncode == 0 else "fail", completed.returncode, log_path
    )

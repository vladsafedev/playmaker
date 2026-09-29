"""Concurrency guarantees for the standalone, system-Python ledger script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "skills/playmaker-coach/scripts/ledger.py"


def _env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PM_LEDGER"] = str(path)
    return env


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEDGER), *args],
        env=_env(path),
        text=True,
        capture_output=True,
        check=False,
    )


def _row(wp: str) -> dict[str, object]:
    return {
        "date": "2026-01-01",
        "repo": "test-repo",
        "wp": wp,
        "class": "feature",
        "risk": "-",
        "impl_lane": "codex",
        "impl_model": "-",
        "gate_first_pass": "-",
        "reviewers": [],
        "blocking_r1": "-",
        "blocking_accepted": "-",
        "blocking_rejected": "-",
        "cycles": "-",
        "rounds": "-",
        "wall_min": "-",
        "quota_note": "-",
        "outcome": "open",
        "commit": "-",
        "note": "-",
        "escaped_from": [],
    }


def test_concurrent_adds_preserve_every_row(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    processes = [
        subprocess.Popen(
            [sys.executable, str(LEDGER), "add", f"wp=add-{i}", "class=feature", "impl_lane=codex"],
            env=_env(path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(20)
    ]

    assert [process.wait() for process in processes] == [0] * 20
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 20
    assert {row["wp"] for row in rows} == {f"add-{i}" for i in range(20)}


def test_concurrent_adds_and_fixes_keep_valid_complete_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        "".join(json.dumps(_row("target" if i == 199 else f"seed-{i}")) + "\n" for i in range(200))
    )
    commands = [
        ["add", f"wp=add-{i}", "class=feature", "impl_lane=codex"]
        for i in range(10)
    ] + [["fix", "wp=target", "note=fixed"] for _ in range(5)]
    processes = [
        subprocess.Popen(
            [sys.executable, str(LEDGER), *command],
            env=_env(path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]

    assert [process.wait() for process in processes] == [0] * len(processes)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 210
    assert next(row for row in rows if row["wp"] == "target")["note"] == "fixed"


def test_readers_skip_a_trailing_partial_json_fragment(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(_row("complete")) + "\n{\"wp\": \"partial")

    tail = _run(path, "tail")
    stats = _run(path, "stats")

    assert tail.returncode == stats.returncode == 0
    assert "complete" in tail.stdout
    assert "1 rows" in stats.stdout
    assert "trailing partial line skipped" in tail.stderr
    assert "trailing partial line skipped" in stats.stderr

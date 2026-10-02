"""Reviewer and lane evidence: the `reviewers` verdict scan and closed-only `stats`."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "skills/playmaker-coach/scripts/ledger.py"


def _env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PM_LEDGER"] = str(path)
    return env


def _run(path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEDGER), *args],
        env=_env(path),
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def _reviewer_key():
    spec = importlib.util.spec_from_file_location("ledger_under_test", LEDGER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.reviewer_key


@pytest.mark.parametrize(
    ("lane", "model", "want"),
    [
        ("agy", "", "agy-gemini-pro"),
        ("agy", "-", "agy-gemini-pro"),
        ("agy", "gemini-3.1-pro-high", "agy-gemini-pro"),
        ("agy", "flash", "agy-flash"),
        ("opencode", "", "glm-5.3"),
        ("opencode", "glm-5.3", "glm-5.3"),
        ("opencode", "other", "other"),
        ("opencode", "a/b", "b"),
        ("kimi", "-", "kimi-k3"),
        ("kimi", "anything", "kimi-k3"),
        ("claude", "", "opus"),
        ("claude", "opus-4", "opus"),
        ("claude", "sonnet", "sonnet"),
        ("codex", "-", "codex"),
        ("muse", "-", "muse"),
        ("opus", "-", "opus"),
    ],
)
def test_reviewer_key(lane: str, model: str, want: str) -> None:
    assert _reviewer_key()(lane, model) == want


def _row(wp: str, **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "date": "2026-10-01",
        "repo": "team",
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
        "outcome": "landed",
        "commit": "-",
        "note": "-",
        "escaped_from": [],
    }
    row.update(kw)
    return row


def _stats_line(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting with {prefix!r}:\n{stdout}")


def test_stats_uses_closed_rows_and_merges_reviewer_seats(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    rows = [
        _row(
            "alpha",
            reviewers=["agy", "agy-gemini-pro"],
            gate_first_pass="y",
            blocking_r1=2,
            blocking_accepted=1,
            cycles=1,
        ),
        _row(
            "beta",
            outcome="open",
            commit="abc123",
            reviewers=["codex"],
            gate_first_pass="n",
            blocking_r1=5,
            cycles=9,
        ),
        _row(
            "gamma",
            reviewers=["codex"],
            gate_first_pass="n",
            blocking_r1=4,
            blocking_accepted=0,
            cycles=3,
        ),
        _row("delta", outcome="abandoned"),
    ]
    rows += [
        _row(f"m{i}", impl_lane="muse", gate_first_pass="y", blocking_r1=1, cycles=2)
        for i in range(5)
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    proc = _run(path, "stats")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout

    bang = _stats_line(out, "! 1 open row(s) carry a commit")
    assert "beta" in bang

    codex = _stats_line(out, "codex / feature")
    assert "67%" in codex  # landed over 3 closed rows; open beta excluded
    assert "50%" in codex  # gate1st over the closed rows with a known gate
    assert "3.0" in codex and "2.0" in codex  # r1 blk / cycles without beta's 5 / 9
    assert codex.rstrip().endswith("*")  # 3 closed rows < 5

    muse = _stats_line(out, "muse / feature")
    assert "100%" in muse
    assert not muse.rstrip().endswith("*")  # 5 closed rows: enough evidence

    assert "* fewer than 5 closed rows" in out

    revs: dict[str, list[str]] = {}
    seen_header = False
    for line in out.splitlines():
        if line.startswith("reviewer (boards sat on"):
            seen_header = True
            continue
        if seen_header and line.strip() and not line.startswith("per-reviewer"):
            parts = line.split()
            revs[parts[0]] = parts[1:]
    assert revs["agy-gemini-pro"] == ["1", "1", "1"]
    assert "agy" not in revs  # agy + agy-gemini-pro in one row merge into one seat
    assert revs["codex"] == ["2", "1", "0"]
    assert "per-reviewer findings by lens: ledger.py reviewers  (run in the repo root;" in out
    assert "default roots = . and ../" in out and "-wt)" in out


def _write_verdict(
    path: Path, reviewer: str | None, verdict: str, findings: list[dict[str, object]]
) -> None:
    data: dict[str, object] = {"verdict": verdict, "findings": findings}
    if reviewer is not None:
        data["reviewer"] = reviewer
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _finding(severity: str, file: str, line: int) -> dict[str, object]:
    return {"severity": severity, "file": file, "line": line}


def _verdict_tree(root: Path) -> None:
    base = root / ".playmaker" / "reviews"
    _write_verdict(
        base / "wp-a" / "verdict-agy-correctness.json",
        "agy/gemini-3.1-pro-high",
        "fail",
        [
            _finding("blocking", "app.py", 10),
            _finding("blocking", "app.py", 100),
            _finding("major", "app.py", 50),
        ],
    )
    _write_verdict(base / "wp-a" / "verdict-codex-correctness.json", "codex/-", "pass", [])
    _write_verdict(
        base / "wp-a" / "verdict-muse-risk.json",
        "muse/-",
        "pass_with_nits",
        [_finding("nit", "notes.py", 3)],
    )
    arch = base / "wp-a" / "archive-20261001T000000Z"
    _write_verdict(
        arch / "verdict-agy-correctness-r2.json",
        "agy/gemini-3.1-pro-high",
        "fail",
        [_finding("blocking", "app.py", 12)],
    )
    _write_verdict(
        arch / "verdict-codex-correctness-r2.json",
        "codex/-",
        "fail",
        [
            _finding("blocking", "app.py", 14),
            _finding("major", "other.py", 5),
            _finding("minor", "x.py", 1),
            _finding("nit", "y.py", 2),
        ],
    )
    # no `reviewer` field: the filename lane still maps to agy-gemini-pro
    _write_verdict(base / "wp-b" / "verdict-agy-correctness.json", None, "pass", [])
    bad = base / "wp-b" / "verdict-kimi-correctness.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json")
    # one canonical reviewer, two seats on one WP+round
    _write_verdict(
        base / "wp-c" / "verdict-muse-contracts.json",
        "muse/-",
        "fail",
        [
            _finding("blocking", "shared.py", 10),
            _finding("blocking", "solo.py", 50),
        ],
    )
    _write_verdict(
        base / "wp-c" / "verdict-muse-risk.json",
        "muse/-",
        "fail",
        [_finding("blocking", "shared.py", 12)],
    )
    _write_verdict(
        base / "wp-d" / "verdict-muse-contracts.json",
        "muse/-",
        "fail",
        [_finding("blocking", "other.py", 30)],
    )
    _write_verdict(base / "wp-d" / "verdict-muse-risk.json", "muse/-", "pass", [])
    # byte-identical minimal verdicts on two WPs: two verdicts, not one
    for wp in ("wp-e", "wp-f"):
        _write_verdict(base / wp / "verdict-codex-contracts.json", None, "pass", [])


def _reviewer_rows(stdout: str) -> list[tuple[str, str, list[int], bool]]:
    rows = []
    in_table = False
    for line in stdout.splitlines():
        if line.startswith("lens / reviewer"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip() or line.startswith("* "):
            break
        parts = line.split()
        assert parts[1] == "/"
        rows.append((parts[2], parts[0], [int(x) for x in parts[3:12]], parts[12:] == ["*"]))
    return rows


def test_reviewers_counts_unique_missed_dedupe_and_skipped(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    _verdict_tree(r1)
    # byte-identical copy of one verdict in a second root: counted once
    src = r1 / ".playmaker" / "reviews" / "wp-e" / "verdict-codex-contracts.json"
    dst = r2 / ".playmaker" / "reviews" / "wp-e" / "verdict-codex-contracts.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())

    single = _run(ledger, "reviewers", f"roots={r1}")
    assert single.returncode == 0, single.stderr
    both = _run(ledger, "reviewers", f"roots={r1},{r2}")
    assert both.returncode == 0, both.stderr
    assert "12 verdict files" in single.stdout.splitlines()[0]
    assert "1 skipped" in single.stdout.splitlines()[0]
    assert "12 verdict files" in both.stdout.splitlines()[0]

    rows = _reviewer_rows(both.stdout)
    assert [(who, lens) for who, lens, _, _ in rows] == [
        ("codex", "contracts"),
        ("muse", "contracts"),
        ("agy-gemini-pro", "correctness"),
        ("codex", "correctness"),
        ("muse", "risk"),
    ]
    by_seat = {(who, lens): (nums, star) for who, lens, nums, star in rows}
    # verdicts, boards, with blk, blk, unique, major, minor, missed same, missed other
    assert by_seat[("agy-gemini-pro", "correctness")] == ([3, 2, 2, 3, 2, 1, 0, 0, 0], True)
    assert by_seat[("codex", "correctness")] == ([2, 1, 1, 1, 0, 1, 2, 1, 0], True)
    assert by_seat[("muse", "risk")] == ([3, 3, 1, 1, 0, 0, 1, 0, 2], True)
    assert by_seat[("muse", "contracts")] == ([2, 2, 2, 3, 2, 0, 0, 0, 0], True)
    assert by_seat[("codex", "contracts")] == ([2, 2, 0, 0, 0, 0, 0, 0, 0], True)
    assert "* fewer than 5 verdicts on this lens" in both.stdout

    risk_only = _run(ledger, "reviewers", f"roots={r1}", "lens=risk")
    assert risk_only.returncode == 0, risk_only.stderr
    assert [(who, lens) for who, lens, _, _ in _reviewer_rows(risk_only.stdout)] == [
        ("muse", "risk")
    ]
    assert "3 verdict files" in risk_only.stdout.splitlines()[0]

    cwd_run = _run(ledger, "reviewers", cwd=r1)
    assert cwd_run.returncode == 0, cwd_run.stderr
    assert "12 verdict files" in cwd_run.stdout.splitlines()[0]


def test_reviewers_wp_is_the_dir_under_the_last_playmaker_reviews(tmp_path: Path) -> None:
    # the root itself sits under a directory named `reviews`
    root = tmp_path / "reviews" / "proj"
    base = root / ".playmaker" / "reviews"
    _write_verdict(base / "wp-x" / "verdict-codex-risk.json", "codex/-", "pass", [])
    _write_verdict(base / "wp-y" / "verdict-codex-risk.json", "codex/-", "fail", [])
    res = _run(tmp_path / "ledger.jsonl", "reviewers", f"roots={root}")
    assert res.returncode == 0, res.stderr
    rows = _reviewer_rows(res.stdout)
    assert [(who, lens, counts[:2]) for who, lens, counts, _ in rows] == [("codex", "risk", [2, 2])]

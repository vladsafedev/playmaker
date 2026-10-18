"""ledger.py --from-session: thread-derived lane/model/wall/gate/cycles fills."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "skills/playmaker-coach/scripts/ledger.py"

SESSIONS_SQL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    agent TEXT,
    model TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    parent_id TEXT,
    gate_status TEXT
)
"""


def _run(ledger: Path, state_db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PM_LEDGER"] = str(ledger)
    env["PLAYMAKER_STATE_DB"] = str(state_db)
    return subprocess.run(
        [sys.executable, str(LEDGER), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _rows(ledger: Path) -> list[dict]:
    return [json.loads(line) for line in ledger.read_text().splitlines()]


def _seed_db(path: Path, *, with_fallback: bool = False) -> dict[str, str]:
    ids = {name: str(uuid.uuid4()) for name in ("root", "c1", "c2", "solo", "live")}
    conn = sqlite3.connect(path)
    conn.execute(SESSIONS_SQL)
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        [
            # a three-session thread: root (codex, gate pass) -> c1 -> c2
            (ids["root"], "codex", "gpt-5.2-coding", "done",
             "2026-10-18T10:00:00+00:00", "2026-10-18T10:40:00+00:00", None, "pass"),
            (ids["c1"], "codex", "gpt-5.2-coding", "done",
             "2026-10-18T10:50:00+00:00", "2026-10-18T11:10:00+00:00", ids["root"], None),
            (ids["c2"], "codex", "gpt-5.2-coding", "done",
             "2026-10-18T11:15:00+00:00", "2026-10-18T11:20:00+00:00", ids["c1"], None),
            # an unrelated single-session thread: gate fail, no model
            (ids["solo"], "claude", None, "failed",
             "2026-10-18T12:00:00+00:00", "2026-10-18T12:07:30+00:00", None, "fail"),
        ],
    )
    if with_fallback:
        conn.execute("ALTER TABLE sessions ADD COLUMN fallback_of TEXT")
        conn.execute(
            "UPDATE sessions SET fallback_of = ? WHERE id = ?",
            ("abcdef1234567890", ids["root"]),
        )
    # still running: wall_min measures from root.started_at to now
    hour_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    conn.execute(
        "INSERT INTO sessions (id, agent, model, status, started_at, finished_at, "
        "parent_id, gate_status) VALUES (?,?,?,?,?,?,?,?)",
        (ids["live"], "kimi", "k3", "running", hour_ago.isoformat(), None, None, None),
    )
    conn.commit()
    conn.close()
    return ids


def test_add_from_session_fills_the_five_fields_from_the_thread(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    ids = _seed_db(db)

    proc = _run(
        ledger, db, "add", "wp=alpha", "class=mechanical", "risk=normal",
        "--from-session", ids["c2"],
    )

    assert proc.returncode == 0, proc.stderr
    row = _rows(ledger)[0]
    assert row["impl_lane"] == "codex"  # the root's agent, not the leaf's
    assert row["impl_model"] == "gpt-5.2-coding"
    assert row["wall_min"] == 80  # 10:00 -> latest finished 11:20
    assert row["gate_first_pass"] == "y"
    assert row["cycles"] == 2  # two resume sessions in the thread
    assert row["quota_note"] == "-"
    assert "[ledger] from-session: impl_lane=codex impl_model=gpt-5.2-coding" in proc.stderr
    assert "wall_min=80" in proc.stderr and "gate_first_pass=y" in proc.stderr
    assert "cycles=2" in proc.stderr


def test_add_from_session_by_unique_prefix_and_gate_fail(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    ids = _seed_db(db)

    proc = _run(
        ledger, db, "add", "wp=beta", "class=feature", "--from-session", ids["solo"][:8]
    )

    assert proc.returncode == 0, proc.stderr
    row = _rows(ledger)[0]
    assert row["impl_lane"] == "claude"
    assert row["impl_model"] == "-"  # NULL model
    assert row["gate_first_pass"] == "n"  # fail
    assert row["wall_min"] == 8  # 12:00 -> 12:07:30 rounds to 8
    assert row["cycles"] == 0


def test_explicit_kv_wins_over_from_session(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    ids = _seed_db(db)

    proc = _run(
        ledger, db,
        "add", "wp=gamma", "class=mechanical", "impl_lane=claude", "wall_min=1",
        "--from-session", ids["root"],
    )

    assert proc.returncode == 0, proc.stderr
    row = _rows(ledger)[0]
    assert row["impl_lane"] == "claude"
    assert row["wall_min"] == 1
    assert row["impl_model"] == "gpt-5.2-coding"  # not given explicitly: still filled
    assert "impl_lane=" not in proc.stderr
    assert "wall_min=" not in proc.stderr
    assert "impl_model=gpt-5.2-coding" in proc.stderr


def test_from_session_unknown_id_exits_1(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    _seed_db(db)

    proc = _run(ledger, db, "add", "wp=x", "class=mechanical", "--from-session", "nope1234")

    assert proc.returncode == 1
    assert "no session" in proc.stderr
    assert not ledger.exists()


def test_from_session_ambiguous_prefix_exits_1(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(SESSIONS_SQL)
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        [
            ("aaaa1111-alpha", "codex", None, "done",
             "2026-10-18T10:00:00+00:00", "2026-10-18T10:10:00+00:00", None, None),
            ("aaaa2222-beta", "codex", None, "done",
             "2026-10-18T10:00:00+00:00", "2026-10-18T10:10:00+00:00", None, None),
        ],
    )
    conn.commit()
    conn.close()

    proc = _run(ledger, db, "add", "wp=x", "class=mechanical", "--from-session", "aaaa")

    assert proc.returncode == 1
    assert "matches 2 sessions" in proc.stderr


def test_from_session_wall_uses_now_while_running(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    ids = _seed_db(db)

    proc = _run(
        ledger, db, "add", "wp=live", "class=mechanical", "--from-session", ids["live"]
    )

    assert proc.returncode == 0, proc.stderr
    wall = _rows(ledger)[0]["wall_min"]
    assert isinstance(wall, int)
    assert 59 <= wall <= 61  # seeded one hour ago


def test_from_session_quota_note_when_fallback_of_exists(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    ids = _seed_db(db, with_fallback=True)

    proc = _run(
        ledger, db, "add", "wp=delta", "class=mechanical", "--from-session", ids["root"]
    )

    assert proc.returncode == 0, proc.stderr
    assert _rows(ledger)[0]["quota_note"] == "fallback_of=abcdef12"
    assert "quota_note=fallback_of=abcdef12" in proc.stderr


def test_fix_from_session_updates_the_last_matching_row(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    db = tmp_path / "state.db"
    ids = _seed_db(db)
    seed = {
        "date": "2026-10-18", "repo": "team", "wp": "fixme", "class": "mechanical",
        "risk": "-", "impl_lane": "?", "impl_model": "?", "gate_first_pass": "?",
        "reviewers": [], "blocking_r1": "-", "blocking_accepted": "-",
        "blocking_rejected": "-", "cycles": "-", "rounds": "-", "wall_min": "-",
        "quota_note": "-", "outcome": "open", "commit": "-", "note": "-",
        "escaped_from": [],
    }
    ledger.write_text(json.dumps(seed) + "\n")

    proc = _run(ledger, db, "fix", "wp=fixme", "--from-session", ids["root"])

    assert proc.returncode == 0, proc.stderr
    row = _rows(ledger)[0]
    assert row["impl_lane"] == "codex"
    assert row["impl_model"] == "gpt-5.2-coding"
    assert row["wall_min"] == 80
    assert row["gate_first_pass"] == "y"
    assert row["cycles"] == 2

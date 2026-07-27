from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.state as state


@pytest.fixture
def db(monkeypatch, tmp_path: Path):
    """Point the module-level paths at a throwaway home and initialise it."""
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()
    return home


def test_init_db_creates_the_directory_layout(db: Path) -> None:
    assert (db / "state.db").is_file()
    assert (db / "logs").is_dir()
    assert (db / "outputs").is_dir()
    assert (db / "agents").is_dir()


def test_init_db_is_idempotent(db: Path) -> None:
    sid = state.insert_session(agent="codex", prompt="p", cwd="/tmp")

    state.init_db()

    assert state.get_session(sid) is not None


def test_insert_and_get_round_trip(db: Path) -> None:
    sid = state.insert_session(
        agent="agy",
        prompt="do the thing",
        cwd="/repo",
        files=["a.py", "b.py"],
        model="Claude Opus 4.6 (Thinking)",
        batch_id="dash",
    )

    row = state.get_session(sid)

    assert row is not None
    assert row["agent"] == "agy"
    assert row["status"] == "pending"
    assert row["model"] == "Claude Opus 4.6 (Thinking)"
    assert row["batch_id"] == "dash"
    assert row["files"] == '["a.py", "b.py"]'


def test_get_session_accepts_the_short_id_the_cli_prints(db: Path) -> None:
    # `playmaker list` shows only the first 8 characters, so lookup by prefix
    # is the normal way a user refers to a session.
    sid = state.insert_session(agent="codex", prompt="p", cwd="/repo")

    row = state.get_session(sid[:8])

    assert row is not None
    assert row["id"] == sid


def test_get_session_unknown_id_returns_none(db: Path) -> None:
    assert state.get_session("nope-not-here") is None


def test_update_session_writes_only_named_fields(db: Path) -> None:
    sid = state.insert_session(agent="claude", prompt="p", cwd="/repo")

    state.update_session(sid, status="done", exit_code=0, cost_usd=0.42)

    row = state.get_session(sid)
    assert row is not None
    assert (row["status"], row["exit_code"], row["cost_usd"]) == ("done", 0, 0.42)
    assert row["prompt"] == "p"


def test_update_session_with_no_fields_is_a_noop(db: Path) -> None:
    sid = state.insert_session(agent="claude", prompt="p", cwd="/repo")

    state.update_session(sid)

    assert state.get_session(sid)["status"] == "pending"


def test_list_sessions_filters_and_orders_newest_first(db: Path) -> None:
    old = state.insert_session(agent="codex", prompt="old", cwd="/repo")
    new = state.insert_session(agent="codex", prompt="new", cwd="/repo")
    other = state.insert_session(agent="agy", prompt="other", cwd="/repo")
    state.update_session(old, status="done", started_at="2020-01-01T00:00:00")
    state.update_session(new, status="running", started_at="2030-01-01T00:00:00")

    assert [r["id"] for r in state.list_sessions(agent="codex")] == [new, old]
    assert [r["id"] for r in state.list_sessions(status="done")] == [old]
    assert {r["id"] for r in state.list_sessions()} == {old, new, other}
    assert len(state.list_sessions(limit=1)) == 1


def test_list_batch_returns_only_that_batch_oldest_first(db: Path) -> None:
    first = state.insert_session(agent="codex", prompt="a", cwd="/r", batch_id="dash")
    second = state.insert_session(agent="agy", prompt="b", cwd="/r", batch_id="dash")
    state.insert_session(agent="claude", prompt="c", cwd="/r", batch_id="other")
    state.update_session(first, started_at="2020-01-01T00:00:00")
    state.update_session(second, started_at="2030-01-01T00:00:00")

    assert [r["id"] for r in state.list_batch("dash")] == [first, second]
    assert state.list_batch("nothing") == []


def test_init_db_migrates_a_database_without_model_and_batch_columns(
    monkeypatch, tmp_path: Path
) -> None:
    # Databases created before 0.3/0.4 lack these columns; init_db must add
    # them in place rather than leaving every query broken.
    home = tmp_path / ".playmaker"
    home.mkdir()
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    legacy = sqlite3.connect(home / "state.db")
    legacy.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, agent_session_id TEXT,
            prompt TEXT NOT NULL, cwd TEXT NOT NULL, files TEXT,
            status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            exit_code INTEGER, cost_usd REAL, duration_seconds REAL,
            output_path TEXT, session_file_path TEXT, parent_id TEXT, pid INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO sessions (id, agent, prompt, cwd, status, started_at) "
        "VALUES ('old-1', 'codex', 'p', '/repo', 'done', '2020-01-01T00:00:00')"
    )
    legacy.commit()
    legacy.close()

    state.init_db()

    row = state.get_session("old-1")
    assert row is not None
    assert row["model"] is None
    assert row["batch_id"] is None
    assert state.insert_session(agent="agy", prompt="p", cwd="/r", batch_id="b")

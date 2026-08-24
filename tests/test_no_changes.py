"""No-change detection around simulated write-capable worker runs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from typer import BadParameter
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.state as state
from playmaker import cli, watcher
from playmaker.agents.base import DispatchResult

runner = CliRunner()


@pytest.fixture
def db(monkeypatch, tmp_path: Path) -> Path:
    """Point module-level state paths at a disposable home."""
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()
    return home


class _Worker:
    def __init__(self, *, writes_file: bool = False) -> None:
        self.writes_file = writes_file

    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model):
        return self._finish(cwd, on_session_started)

    def resume(self, prompt, cwd, agent_session_id, files, *, on_session_started, model):
        return self._finish(cwd, on_session_started)

    def _finish(self, cwd: Path, on_session_started) -> DispatchResult:
        on_session_started("fake-agent-session")
        if self.writes_file:
            (cwd / "worker-output.txt").write_text("changed", encoding="utf-8")
        return DispatchResult(
            agent_session_id="fake-agent-session",
            cwd=str(cwd),
            session_file=None,
            initial_output="worker finished",
        )


def _git_dir(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    return path


def _already_dirty_git_file(tmp_path: Path) -> tuple[Path, Path]:
    cwd = _git_dir(tmp_path / "repo")
    path = cwd / "dirty.txt"
    path.write_text("staged", encoding="utf-8")
    subprocess.run(["git", "-C", str(cwd), "add", path.name], check=True)
    path.write_text("modified before snapshot", encoding="utf-8")
    return cwd, path


def _latest_session() -> dict:
    return state.list_sessions(limit=1)[0]


def test_write_prompt_with_no_worker_changes_is_no_changes_and_notifies_loudly(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    cwd = _git_dir(tmp_path / "repo")
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    notifications: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        cli.notify,
        "notify",
        lambda title, message, **kwargs: notifications.append((title, message, kwargs)),
    )

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "Implement the requested feature.",
            "--cwd",
            str(cwd),
            "--batch",
            "write-batch",
            "--sync",
        ],
    )

    assert result.exit_code == 0, result.output
    row = _latest_session()
    assert row["status"] == "no_changes"
    assert row["files_changed"] == 0
    assert row["pre_tree_hash"]
    assert row["post_tree_hash"]
    get_output = runner.invoke(cli.app, ["get", row["id"]]).output
    summary_output = runner.invoke(cli.app, ["summary", row["id"]]).output
    assert "changes: 0 files (⚠ no_changes)" in get_output
    assert "changes: 0 files (⚠ no_changes)" in summary_output
    summary_json = json.loads(runner.invoke(cli.app, ["summary", row["id"], "--json"]).output)
    assert summary_json["files_changed"] == 0
    assert summary_json["expect_changes"] == 1
    assert summary_json["pre_tree_hash"]
    assert summary_json["post_tree_hash"]
    assert any(
        "done but wrote 0 files" in message and kwargs["sound_name"] == "Basso"
        for _, message, kwargs in notifications
    )
    assert any("0/1 done · fake ⚠ no_changes" in message for _, message, _ in notifications)


def test_read_only_zero_change_run_stays_done_with_only_normal_notification(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    cwd = _git_dir(tmp_path / "repo")
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    notifications: list[dict] = []
    monkeypatch.setattr(
        cli.notify,
        "notify",
        lambda title, message, **kwargs: notifications.append(kwargs),
    )

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "Implement nothing; just inspect the repository.",
            "--cwd",
            str(cwd),
            "--read-only",
            "--sync",
        ],
    )

    assert result.exit_code == 0, result.output
    row = _latest_session()
    assert row["status"] == "done"
    assert row["expect_changes"] == 0
    assert row["files_changed"] == 0
    assert notifications == [
        {
            "sound_name": "Blow",
            "open_path": row["output_path"],
            "group": f"playmaker-{row['id']}",
        }
    ]


def test_worker_changes_one_file_and_get_reports_the_singular_count(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    cwd = _git_dir(tmp_path / "repo")
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker(writes_file=True))
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt", "Create the deliverable.", "--cwd", str(cwd), "--sync"],
    )

    assert result.exit_code == 0, result.output
    row = _latest_session()
    assert row["status"] == "done"
    assert row["files_changed"] == 1
    get_result = runner.invoke(cli.app, ["get", row["id"]])
    assert get_result.exit_code == 0
    assert "changes: 1 file" in get_result.output
    summary_result = runner.invoke(cli.app, ["summary", row["id"]])
    assert summary_result.exit_code == 0
    assert "changes: 1 file" in summary_result.output
    get_json = json.loads(runner.invoke(cli.app, ["get", row["id"], "--json"]).output)
    assert get_json["files_changed"] == 1
    assert get_json["expect_changes"] == 1
    assert get_json["pre_tree_hash"]
    assert get_json["post_tree_hash"]


def test_non_git_mtime_fallback_counts_a_written_file_without_raising(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "plain-directory"
    cwd.mkdir()
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker(writes_file=True))
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt", "Write a file.", "--cwd", str(cwd), "--sync"],
    )

    assert result.exit_code == 0, result.output
    row = _latest_session()
    assert isinstance(row["files_changed"], int)
    assert row["files_changed"] == 1


def test_git_snapshot_counts_a_second_edit_to_an_already_dirty_file(tmp_path: Path) -> None:
    cwd, path = _already_dirty_git_file(tmp_path)
    before = state.take_working_tree_snapshot(cwd)

    path.write_text("modified by worker", encoding="utf-8")
    written = path.stat()
    os.utime(path, ns=(written.st_atime_ns, written.st_mtime_ns + 1_000_000))
    after = state.take_working_tree_snapshot(cwd)

    assert state.count_working_tree_changes(before, after) >= 1
    assert before.tree_hash != after.tree_hash


def test_git_snapshot_keeps_an_untouched_dirty_file_at_zero_changes(tmp_path: Path) -> None:
    cwd, _ = _already_dirty_git_file(tmp_path)

    before = state.take_working_tree_snapshot(cwd)
    after = state.take_working_tree_snapshot(cwd)

    assert state.count_working_tree_changes(before, after) == 0


def test_git_snapshot_counts_an_untracked_file_deleted_by_the_worker(tmp_path: Path) -> None:
    cwd = _git_dir(tmp_path / "repo")
    path = cwd / "untracked.txt"
    path.write_text("present before", encoding="utf-8")
    before = state.take_working_tree_snapshot(cwd)

    path.unlink()
    after = state.take_working_tree_snapshot(cwd)

    assert state.count_working_tree_changes(before, after) == 1


@pytest.mark.parametrize(
    ("prompt", "explicit", "expected"),
    [
        ("Implement a feature", None, True),
        ("Please fix the parser", None, True),
        ("Implement X in foo.py. Paste the pytest output in your final answer.", None, True),
        ("Recon only: implement nothing", None, False),
        ("Read only and reply with a summary", None, False),
        ("Answer only with a summary", None, False),
        ("Do not modify files; explain the code", None, False),
        ("Explain the code", None, False),
        ("Answer this question", True, True),
        ("Create a file", False, False),
    ],
)
def test_expects_changes_heuristic(prompt: str, explicit: bool | None, expected: bool) -> None:
    assert state.expects_changes(prompt, explicit) is expected


def test_both_expectation_flags_are_rejected(db: Path, monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(BadParameter, match="cannot be used together"):
        cli._expectation_from_flags(True, True)

    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "Write a file.",
            "--cwd",
            str(cwd),
            "--expect-changes",
            "--read-only",
        ],
    )

    assert result.exit_code != 0
    assert "cannot be used together" in result.output


def test_list_filters_no_changes_and_the_status_is_terminal(db: Path) -> None:
    sid = state.insert_session(agent="fake", prompt="p", cwd="/repo")
    state.update_session(sid, status="no_changes", files_changed=0)

    result = runner.invoke(cli.app, ["list", "--status", "no_changes", "--json"])

    assert result.exit_code == 0
    assert [row["id"] for row in json.loads(result.output)] == [sid]
    assert "no_changes" in state.TERMINAL_STATUSES
    assert watcher._ICONS["no_changes"] == "⚠"


def test_continue_inherits_the_write_expectation(db: Path, monkeypatch, tmp_path: Path) -> None:
    cwd = _git_dir(tmp_path / "repo")
    parent = state.insert_session(
        agent="fake", prompt="Implement it", cwd=str(cwd), expect_changes=True
    )
    state.update_session(parent, status="done", agent_session_id="parent-agent-session")
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.app,
        ["continue", parent, "--prompt", "Try again.", "--sync"],
    )

    assert result.exit_code == 0, result.output
    child = _latest_session()
    assert child["parent_id"] == parent
    assert child["expect_changes"] == 1
    assert child["status"] == "no_changes"


def test_init_db_migrates_no_changes_columns_idempotently(monkeypatch, tmp_path: Path) -> None:
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
            output_path TEXT, session_file_path TEXT, parent_id TEXT, pid INTEGER,
            model TEXT, batch_id TEXT, batch_notified INTEGER
        )
        """
    )
    legacy.commit()
    legacy.close()

    state.init_db()
    state.init_db()

    with state.connect() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(sessions)")]
    for column in ("files_changed", "pre_tree_hash", "post_tree_hash", "expect_changes"):
        assert columns.count(column) == 1

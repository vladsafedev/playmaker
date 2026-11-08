from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker import cli, state


@pytest.fixture
def db(monkeypatch, tmp_path):
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()


class RemoteHandler:
    def __init__(self, calls, error=None):
        self.calls = calls
        self.error = error

    def stop_remote(self, task_id):
        self.calls.append(("remote", task_id))
        if self.error:
            raise RuntimeError(self.error)
        return "stopped"


def _session(agent="manus", status="running", pid=4242, task_id="task-1"):
    sid = state.insert_session(agent=agent, prompt="p", cwd="/repo")
    state.update_session(sid, status=status, pid=pid, agent_session_id=task_id)
    return sid


def test_running_manus_kills_local_then_remote(db, monkeypatch):
    calls = []
    sid = _session()
    monkeypatch.setattr(cli, "get_handler", lambda agent: RemoteHandler(calls))
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: calls.append(("local", pgid)))

    result = CliRunner().invoke(cli.app, ["kill", sid])

    assert result.exit_code == 0
    assert calls == [("local", 4242), ("remote", "task-1")]
    assert state.get_session(sid)["status"] == "killed"
    assert "manus task task-1: stopped" in result.output


def test_running_manus_without_pid_still_stops_remote(db, monkeypatch):
    calls = []
    sid = _session(pid=None)
    monkeypatch.setattr(cli, "get_handler", lambda agent: RemoteHandler(calls))
    result = CliRunner().invoke(cli.app, ["kill", sid])
    assert result.exit_code == 0
    assert calls == [("remote", "task-1")]
    assert state.get_session(sid)["status"] == "killed"


def test_done_manus_stops_remote_without_changing_row(db, monkeypatch):
    calls = []
    sid = _session(status="done")
    monkeypatch.setattr(cli, "get_handler", lambda agent: RemoteHandler(calls))
    result = CliRunner().invoke(cli.app, ["kill", sid])
    assert result.exit_code == 0
    assert calls == [("remote", "task-1")]
    assert state.get_session(sid)["status"] == "done"
    assert "session is done; manus task task-1: stopped" in result.output


def test_done_manus_kills_running_sibling_before_remote(db, monkeypatch):
    calls = []
    done = _session(status="done")
    sibling = _session(pid=5151)
    monkeypatch.setattr(cli, "get_handler", lambda agent: RemoteHandler(calls))
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: calls.append(("local", pgid)))

    result = CliRunner().invoke(cli.app, ["kill", done])

    assert result.exit_code == 0
    assert calls == [("local", 5151), ("remote", "task-1")]
    assert state.get_session(done)["status"] == "done"
    assert state.get_session(sibling)["status"] == "killed"
    assert f"killed {sibling} (pid 5151)" in result.output


def test_done_codex_keeps_existing_behavior(db, monkeypatch):
    sid = _session(agent="codex", status="done")
    monkeypatch.setattr(cli, "get_handler", lambda agent: object())
    result = CliRunner().invoke(cli.app, ["kill", sid])
    assert result.exit_code == 0
    assert "session is done; nothing to kill" in result.output


def test_remote_failure_redacts_key_and_marks_killed(db, monkeypatch):
    sentinel = "secret-that-must-not-leak"
    sid = _session()
    error = f"bad [redacted] {sentinel}".replace(sentinel, "[redacted]")
    monkeypatch.setattr(
        cli, "get_handler", lambda agent: RemoteHandler([], error)
    )
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: None)
    result = CliRunner().invoke(cli.app, ["kill", sid])
    assert result.exit_code == 1
    assert sentinel not in result.output
    assert "stop failed" in result.output
    assert state.get_session(sid)["status"] == "killed"


def test_remote_failure_escapes_rich_markup(db, monkeypatch):
    sid = _session()
    monkeypatch.setattr(
        cli,
        "get_handler",
        lambda agent: RemoteHandler([], "failed at [/v2/task.stop]"),
    )
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: None)

    result = CliRunner().invoke(cli.app, ["kill", sid])

    assert result.exit_code == 1
    assert "failed at [/v2/task.stop]" in result.output
    assert state.get_session(sid)["status"] == "killed"


def test_unregistered_lane_is_killed_locally(db, monkeypatch):
    calls = []
    sid = _session(agent="retired-lane")

    def unknown(agent):
        raise KeyError(f"unknown agent {agent!r}")

    monkeypatch.setattr(cli, "get_handler", unknown)
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: calls.append(("local", pgid)))

    result = CliRunner().invoke(cli.app, ["kill", sid])

    assert result.exit_code == 0
    assert calls == [("local", 4242)]
    assert state.get_session(sid)["status"] == "killed"

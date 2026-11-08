"""`dispatch --fallback LANE[,LANE]`: validation, rerouting, and its limits."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.state as state
from playmaker import cli
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
    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model, **kwargs):
        on_session_started("fake-agent-session")
        return DispatchResult(
            agent_session_id="fake-agent-session",
            cwd=str(cwd),
            session_file=None,
            initial_output="worker finished",
        )

    def resume(self, prompt, cwd, agent_session_id, files, *, on_session_started, model, **kwargs):
        return self.dispatch(prompt, cwd, files, on_session_started=on_session_started, model=model)


class _FailingWorker:
    def __init__(self, message: str) -> None:
        self.message = message

    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model, **kwargs):
        raise RuntimeError(self.message)

    def resume(self, prompt, cwd, agent_session_id, files, *, on_session_started, model, **kwargs):
        raise RuntimeError(self.message)


class _SpawnRecorder:
    def __init__(self) -> None:
        self.sids: list[str] = []

    def __call__(self, sid: str) -> int:
        self.sids.append(sid)
        state.update_session(sid, status="running", pid=4242)
        return 4242


def _notifications(monkeypatch) -> list[tuple[str, str, dict]]:
    sent: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        cli.notify, "notify", lambda title, message, **kw: sent.append((title, message, kw))
    )
    return sent


def _latest_session() -> dict:
    return state.list_sessions(limit=1)[0]


def test_unknown_fallback_lane_is_rejected_before_anything_runs(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)

    def registry_like_handler(name: str):
        if name not in ("codex", "claude", "muse"):
            raise KeyError(f"unknown agent {name!r}; available: codex, claude, muse")
        return _Worker()

    monkeypatch.setattr(cli, "get_handler", registry_like_handler)

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "Do the thing.",
            "--cwd",
            str(tmp_path / "repo"),
            "--fallback",
            "nosuchlane",
        ],
    )

    assert result.exit_code == 1
    assert state.list_sessions() == []
    assert spawn.sids == []


def test_dispatch_stores_the_lane_list_and_spawns_through_the_helper(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "Implement it.",
            "--cwd",
            str(cwd),
            "--fallback",
            "claude, muse",
        ],
    )

    assert result.exit_code == 0, result.output
    row = _latest_session()
    assert json.loads(row["fallback_lanes"]) == ["claude", "muse"]
    assert spawn.sids == [row["id"]]


def _detached_failure(
    tmp_path: Path,
    *,
    lanes: list[str],
    started_at: str | None = None,
    batch_id: str | None = "fb",
) -> str:
    """Insert a running session whose handler is already patched to fail."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = state.insert_session(
        agent="codex", prompt="Implement it.", cwd=str(cwd), batch_id=batch_id, expect_changes=True
    )
    if lanes:
        state.update_session(sid, fallback_lanes=json.dumps(lanes))
    if started_at:
        state.update_session(sid, started_at=started_at)
    state.update_session(sid, status="running", pid=111)
    result = runner.invoke(cli.app, ["_run-detached", sid])
    assert result.exit_code == 1
    return sid


def test_quota_failure_reroutes_to_the_next_lane_and_links_both_rows(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    sent = _notifications(monkeypatch)
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("usage limit reached"))

    sid = _detached_failure(tmp_path, lanes=["claude", "muse"])

    old = state.get_session(sid)
    assert old["status"] == "failed"
    assert old["fail_reason"] == "quota"
    assert old["fell_back_to"] is not None
    new = state.get_session(old["fell_back_to"])
    assert new["agent"] == "claude"
    assert new["prompt"] == old["prompt"]
    assert new["cwd"] == old["cwd"]
    assert new["batch_id"] == "fb"
    assert new["parent_id"] == sid
    assert new["fallback_of"] == sid
    assert new["model"] is None
    assert json.loads(new["fallback_lanes"]) == ["muse"]
    assert spawn.sids == [new["id"]]
    fallback_events = [
        json.loads(line)
        for line in runner.invoke(cli.app, ["events", "--kind", "fallback"]).output.splitlines()
        if line.strip()
    ]
    assert fallback_events == [
        {
            "id": fallback_events[0]["id"],
            "ts": fallback_events[0]["ts"],
            "session_id": sid,
            "agent": "codex",
            "model": None,
            "batch_id": "fb",
            "kind": "fallback",
            "status": None,
            "prev_status": None,
            "detail": {"from": sid, "to": new["id"], "reason": "quota", "lane": "claude"},
        }
    ]
    assert any("rerouted to claude" in message for _, message, _ in sent)
    old_get = runner.invoke(cli.app, ["get", sid]).output
    new_get = runner.invoke(cli.app, ["get", new["id"]]).output
    assert "fell back to:" in old_get
    assert "fallback of:" in new_get
    old_json = json.loads(runner.invoke(cli.app, ["get", sid, "--json"]).output)
    new_json = json.loads(runner.invoke(cli.app, ["get", new["id"], "--json"]).output)
    assert old_json["fell_back_to"] == new["id"]
    assert new_json["fallback_of"] == sid


def test_unknown_failure_inside_the_window_still_reroutes(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    _notifications(monkeypatch)
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("boom"))

    sid = _detached_failure(tmp_path, lanes=["claude"])

    old = state.get_session(sid)
    assert old["fail_reason"] == "unknown"
    assert old["fell_back_to"] is not None
    assert spawn.sids == [old["fell_back_to"]]
    new = state.get_session(old["fell_back_to"])
    assert json.loads(new["fallback_lanes"] or "[]") == []


def test_late_unknown_failure_does_not_reroute(db: Path, monkeypatch, tmp_path: Path) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    _notifications(monkeypatch)
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("boom"))
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    sid = _detached_failure(tmp_path, lanes=["claude"], started_at=an_hour_ago)

    old = state.get_session(sid)
    assert old["fail_reason"] == "unknown"
    assert old["fell_back_to"] is None
    assert len(state.list_sessions()) == 1
    assert spawn.sids == []


def test_classified_failure_reroutes_even_when_late(db: Path, monkeypatch, tmp_path) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    _notifications(monkeypatch)
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("token expired"))
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    sid = _detached_failure(tmp_path, lanes=["claude"], started_at=an_hour_ago)

    assert state.get_session(sid)["fell_back_to"] is not None
    assert spawn.sids


def test_empty_remaining_list_means_no_reroute(db: Path, monkeypatch, tmp_path: Path) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    _notifications(monkeypatch)
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("429 too many requests"))

    sid = _detached_failure(tmp_path, lanes=[])

    old = state.get_session(sid)
    assert old["fail_reason"] == "quota"
    assert old["fell_back_to"] is None
    assert spawn.sids == []


def test_sync_dispatch_never_reroutes(db: Path, monkeypatch, tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    _notifications(monkeypatch)
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("usage limit reached"))

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "Implement it.",
            "--cwd",
            str(cwd),
            "--fallback",
            "claude",
            "--sync",
        ],
    )

    assert result.exit_code == 1
    row = _latest_session()
    assert row["status"] == "failed"
    assert row["fell_back_to"] is None
    assert len(state.list_sessions()) == 1
    assert spawn.sids == []


def test_reroute_keeps_the_batch_open_until_the_replacement_lands(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    spawn = _SpawnRecorder()
    monkeypatch.setattr(cli, "_spawn_detached", spawn)
    _notifications(monkeypatch)
    monkeypatch.setattr(
        cli,
        "get_handler",
        lambda name: _FailingWorker("usage limit reached") if name == "codex" else _Worker(),
    )
    sid = _detached_failure(tmp_path, lanes=["claude"])

    # The failed session rerouted, so the batch is not drained: no batch event yet.
    drained = [
        json.loads(line)
        for line in runner.invoke(cli.app, ["events", "--batch", "fb", "--kind", "batch"])
        .output.splitlines()
        if line.strip()
    ]
    assert drained == []

    replacement = state.get_session(sid)["fell_back_to"]
    result = runner.invoke(cli.app, ["_run-detached", replacement])

    assert result.exit_code == 0, result.output
    final = [
        json.loads(line)
        for line in runner.invoke(cli.app, ["events", "--batch", "fb", "--kind", "batch"])
        .output.splitlines()
        if line.strip()
    ]
    assert len(final) == 1
    # The replacement writes nothing while expecting changes, so it lands as
    # no_changes — the point is that the batch event waited for it.
    assert final[0]["detail"] == {"total": 2, "done": 0, "failed": 1, "no_changes": 1}

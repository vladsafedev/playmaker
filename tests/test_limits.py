"""[limits] guard: max_parallel refusal, RAM probe skip and the `limits` command."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.state as state
from playmaker import cli, config, limits
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

    def dispatch(self, prompt, cwd, files, *, on_session_started, model):
        return self._finish(cwd, on_session_started)

    def resume(self, prompt, cwd, agent_session_id, files, *, on_session_started, model):
        return self._finish(cwd, on_session_started)

    def _finish(self, cwd: Path, on_session_started) -> DispatchResult:
        on_session_started("fake-agent-session")
        return DispatchResult(
            agent_session_id="fake-agent-session",
            cwd=str(cwd),
            session_file=None,
            initial_output="worker finished",
        )


@pytest.fixture
def quiet(monkeypatch, tmp_path: Path) -> Path:
    """A fake agent plus silenced notifications; returns a usable --cwd."""
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    return cwd


def _set_limits(monkeypatch, **values: object) -> None:
    monkeypatch.setattr(config, "load_config", lambda: {"limits": values})


def _seed_running(n: int, agent: str = "codex") -> list[str]:
    ids = []
    for _ in range(n):
        sid = state.insert_session(agent=agent, prompt="p", cwd="/repo")
        state.update_session(sid, status="running")
        ids.append(sid)
    return ids


def test_fourth_dispatch_is_refused_with_the_running_list(
    db: Path, quiet: Path, monkeypatch
) -> None:
    _set_limits(monkeypatch, max_parallel=3)
    ids = _seed_running(3)

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "Implement it.", "--cwd", str(quiet)]
    )

    assert result.exit_code == 3
    for sid in ids:
        assert sid[:8] in result.output
    assert "codex" in result.output
    assert "--force" in result.output
    assert len(state.list_sessions(limit=100)) == 3  # no row was inserted


def test_force_bypasses_max_parallel(db: Path, quiet: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, max_parallel=3)
    _seed_running(3)

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "Implement it.",
            "--cwd",
            str(quiet),
            "--sync",
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(state.list_sessions(limit=100)) == 4


def test_below_max_parallel_is_allowed_without_force(db: Path, quiet: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, max_parallel=3)
    _seed_running(2)

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "Implement it.", "--cwd", str(quiet), "--sync"]
    )

    assert result.exit_code == 0, result.output


def test_low_free_ram_refuses_and_force_bypasses(db: Path, quiet: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, min_free_ram_gb=8.0)
    _seed_running(1, agent="claude")
    monkeypatch.setattr(limits, "free_ram_gb", lambda: 1.25)

    result = runner.invoke(
        cli.app, ["dispatch", "claude", "--prompt", "Implement it.", "--cwd", str(quiet)]
    )
    assert result.exit_code == 3
    assert "1.2 GB" in result.output
    assert "min_free_ram_gb" in result.output
    assert "--force" in result.output
    assert len(state.list_sessions(limit=100)) == 1

    forced = runner.invoke(
        cli.app,
        [
            "dispatch",
            "claude",
            "--prompt",
            "Implement it.",
            "--cwd",
            str(quiet),
            "--sync",
            "--force",
        ],
    )
    assert forced.exit_code == 0, forced.output


def test_ram_check_is_skipped_on_an_unknown_platform(db: Path, quiet: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, min_free_ram_gb=8.0)
    monkeypatch.setattr(limits, "_system", lambda: "Plan9")
    assert limits.free_ram_gb() is None

    decision = limits.evaluate()
    assert decision.free_gb is None
    assert decision.allowed

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "Implement it.", "--cwd", str(quiet), "--sync"]
    )
    assert result.exit_code == 0, result.output


def test_limits_off_by_default_allows_any_parallelism(
    db: Path, quiet: Path, monkeypatch
) -> None:
    _set_limits(monkeypatch)
    _seed_running(9)

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "Implement it.", "--cwd", str(quiet), "--sync"]
    )
    assert result.exit_code == 0, result.output


def test_concurrent_inserts_at_the_limit_admit_exactly_one(db: Path) -> None:
    """The count and the insert must be one atomic step.

    Two simultaneous dispatches at running=2/max_parallel=3 both used to count
    the same two sessions and both insert; the BEGIN IMMEDIATE transaction in
    insert_session serializes them so the loser counts the winner and refuses.
    """
    for _ in range(2):
        sid = state.insert_session(agent="codex", prompt="p", cwd="/repo")
        state.update_session(sid, status="running")

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, int]] = []

    def _insert() -> None:
        barrier.wait()  # both threads count at the same moment
        try:
            state.insert_session(agent="codex", prompt="p", cwd="/repo", max_active=3)
            outcomes.append(("inserted", 0))
        except state.LimitExceeded as exc:
            outcomes.append(("refused", len(exc.running_ids)))

    threads = [threading.Thread(target=_insert) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == [("inserted", 0), ("refused", 3)]
    active = len(state.list_sessions(status="running", limit=100)) + len(
        state.list_sessions(status="pending", limit=100)
    )
    assert active == 3  # 2 seeded + the one winner; the loser inserted nothing


def test_continue_is_guarded_too(db: Path, quiet: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, max_parallel=2)
    parent = state.insert_session(agent="fake", prompt="first", cwd=str(quiet))
    state.update_session(parent, status="done", agent_session_id="parent-agent-session")
    _seed_running(2, agent="fake")

    result = runner.invoke(cli.app, ["continue", parent, "--prompt", "Try again."])

    assert result.exit_code == 3
    assert "--force" in result.output
    assert len(state.list_sessions(limit=100)) == 3  # no resume row was inserted

    forced = runner.invoke(
        cli.app, ["continue", parent, "--prompt", "Try again.", "--sync", "--force"]
    )
    assert forced.exit_code == 0, forced.output


def test_limits_command_reports_running_count_and_decision(db: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, max_parallel=3, min_free_ram_gb=8.0)
    _seed_running(1)
    monkeypatch.setattr(limits, "free_ram_gb", lambda: 12.5)

    result = runner.invoke(cli.app, ["limits", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["running"] == 1
    assert payload["max_parallel"] == 3
    assert payload["free_ram_gb"] == 12.5
    assert payload["min_free_ram_gb"] == 8.0
    assert payload["allowed"] is True


def test_limits_command_json_reports_refusal(db: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, max_parallel=1)
    _seed_running(2)
    monkeypatch.setattr(limits, "free_ram_gb", lambda: None)

    result = runner.invoke(cli.app, ["limits", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["running"] == 2
    assert payload["allowed"] is False
    assert any("max_parallel=1" in refusal for refusal in payload["refusals"])


def test_limits_command_human_output(db: Path, monkeypatch) -> None:
    _set_limits(monkeypatch, max_parallel=1)
    _seed_running(2)
    monkeypatch.setattr(limits, "free_ram_gb", lambda: None)

    result = runner.invoke(cli.app, ["limits"])

    assert result.exit_code == 0
    assert "running:         2" in result.output
    assert "max_parallel:    1" in result.output
    assert "unknown (no probe on this platform)" in result.output
    assert "refused" in result.output

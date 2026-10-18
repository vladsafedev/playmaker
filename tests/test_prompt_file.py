"""--prompt-file on dispatch and continue: the prompt comes from a file, not argv."""

from __future__ import annotations

import sys
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


def _latest_session() -> dict:
    return state.list_sessions(limit=1)[0]


def test_prompt_file_content_is_the_stored_prompt(db: Path, quiet: Path, tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(
        "Implement the file-based feature.\nSecond line with `backticks` and $VARS.\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt-file", str(prompt_file), "--cwd", str(quiet), "--sync"],
    )

    assert result.exit_code == 0, result.output
    assert _latest_session()["prompt"] == prompt_file.read_text(encoding="utf-8")


def test_both_prompt_and_prompt_file_are_rejected(db: Path, quiet: Path, tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("from file", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "from argv",
            "--prompt-file",
            str(prompt_file),
            "--cwd",
            str(quiet),
        ],
    )

    assert result.exit_code == 2
    assert "not both" in result.output
    assert state.list_sessions() == []


def test_neither_prompt_nor_prompt_file_is_rejected(db: Path, quiet: Path) -> None:
    result = runner.invoke(cli.app, ["dispatch", "fake", "--cwd", str(quiet)])

    assert result.exit_code == 2
    assert "--prompt" in result.output
    assert state.list_sessions() == []


def test_empty_prompt_file_is_rejected(db: Path, quiet: Path, tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt-file", str(prompt_file), "--cwd", str(quiet)],
    )

    assert result.exit_code == 2
    assert "empty" in result.output
    assert state.list_sessions() == []


def test_missing_prompt_file_is_rejected(db: Path, quiet: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt-file", str(tmp_path / "nope.md"), "--cwd", str(quiet)],
    )

    assert result.exit_code == 2
    assert state.list_sessions() == []


def test_continue_accepts_prompt_file_and_resolves_the_same_rules(
    db: Path, quiet: Path, tmp_path: Path
) -> None:
    parent = state.insert_session(agent="fake", prompt="first", cwd=str(quiet))
    state.update_session(parent, status="done", agent_session_id="parent-agent-session")
    prompt_file = tmp_path / "follow-up.md"
    prompt_file.write_text("Fix the parser now.", encoding="utf-8")

    result = runner.invoke(
        cli.app, ["continue", parent, "--prompt-file", str(prompt_file), "--sync"]
    )

    assert result.exit_code == 0, result.output
    child = _latest_session()
    assert child["parent_id"] == parent
    assert child["prompt"] == "Fix the parser now."

    both = runner.invoke(
        cli.app,
        ["continue", parent, "--prompt", "x", "--prompt-file", str(prompt_file)],
    )
    assert both.exit_code == 2
    assert "not both" in both.output

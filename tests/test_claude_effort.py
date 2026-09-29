"""Forwarding of `[agents.claude] effort` / `--effort` to the claude lane."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
import playmaker.state as state
from playmaker import cli
from playmaker.agents.claude import EFFORT_ENV_VAR, ClaudeHandler, effort_args

runner = CliRunner()


@pytest.fixture
def configured(monkeypatch):
    """Install a fake config.toml body for the duration of a test."""

    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


@pytest.fixture(autouse=True)
def _clean_effort_env(monkeypatch):
    """resolve_effort reads real os.environ — keep the developer's shell out."""
    monkeypatch.delenv(EFFORT_ENV_VAR, raising=False)


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


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> str:
        return "".join(self._lines)


class _FakePopen:
    """Stand-in for subprocess.Popen over claude's stream-json output."""

    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self._lines = lines
        self.returncode = returncode
        self._stderr = stderr
        self.cmd: list[str] = []
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdout = _FakeStream(self._lines)
        self.stderr = _FakeStream([self._stderr])
        return self

    def wait(self) -> int:
        return self.returncode


def _success_lines() -> list[str]:
    return [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-123"}) + "\n",
        json.dumps({"type": "result", "subtype": "success", "result": "ok"}) + "\n",
    ]


def _dispatch_with(cmd_sink: _FakePopen, monkeypatch, tmp_path: Path, **kwargs):
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", cmd_sink)
    return ClaudeHandler().dispatch(prompt="p", cwd=tmp_path, **kwargs)


class _FakeCompleted:
    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.returncode = 0
        self.stdout = json.dumps(
            {
                "session_id": "sess-123",
                "result": "ok",
                "total_cost_usd": 0.01,
                "duration_ms": 100,
            }
        )
        self.stderr = ""


def _resume_with(recorded: list, monkeypatch, tmp_path: Path, **kwargs):
    def _fake_run(cmd, **kwargs):
        recorded.append(cmd)
        return _FakeCompleted(cmd)

    monkeypatch.setattr("playmaker.agents.claude.subprocess.run", _fake_run)
    return ClaudeHandler().resume(prompt="p", cwd=tmp_path, agent_session_id="sess-123", **kwargs)


# ---- config value -----------------------------------------------------------


def test_config_value_follows_model_in_dispatch(configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude": {"effort": "xhigh"}}})
    fake = _FakePopen(_success_lines())

    _dispatch_with(fake, monkeypatch, tmp_path, model="sonnet")

    assert fake.cmd[fake.cmd.index("--model") + 1] == "sonnet"
    assert fake.cmd[fake.cmd.index("--model") + 2 : fake.cmd.index("--model") + 4] == [
        "--effort",
        "xhigh",
    ]


def test_config_value_without_model(configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude": {"effort": "high"}}})
    fake = _FakePopen(_success_lines())

    _dispatch_with(fake, monkeypatch, tmp_path)

    assert "--model" not in fake.cmd
    assert fake.cmd[-3:-1] == ["--effort", "high"]
    assert fake.cmd[-1] == "p"  # the positional prompt still goes last


def test_no_config_no_flag(configured, monkeypatch, tmp_path) -> None:
    configured({})
    fake = _FakePopen(_success_lines())

    _dispatch_with(fake, monkeypatch, tmp_path, model="sonnet")

    assert "--effort" not in fake.cmd
    assert effort_args() == []


def test_invalid_config_raises_before_spawn(configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude": {"effort": "ultra"}}})
    fake = _FakePopen(_success_lines())

    with pytest.raises(RuntimeError, match="ultra"):
        _dispatch_with(fake, monkeypatch, tmp_path)

    assert fake.cmd == []  # no process started


def test_invalid_config_names_the_valid_values(configured) -> None:
    configured({"agents": {"claude": {"effort": "ultra"}}})

    with pytest.raises(RuntimeError, match="low, medium, high, xhigh, max"):
        effort_args()


# ---- resume -----------------------------------------------------------------


def test_resume_appends_effort_after_model(configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude": {"effort": "xhigh"}}})
    recorded: list = []

    _resume_with(recorded, monkeypatch, tmp_path, model="sonnet")

    (cmd,) = recorded
    assert cmd[cmd.index("--model") + 2 : cmd.index("--model") + 4] == ["--effort", "xhigh"]


def test_resume_invalid_raises_before_spawn(configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude": {"effort": "bogus"}}})
    recorded: list = []

    with pytest.raises(RuntimeError, match="bogus"):
        _resume_with(recorded, monkeypatch, tmp_path)

    assert recorded == []


# ---- CLI override -----------------------------------------------------------


def test_cli_override_beats_config(configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude": {"effort": "low"}}})
    monkeypatch.setenv(EFFORT_ENV_VAR, "xhigh")
    fake = _FakePopen(_success_lines())

    _dispatch_with(fake, monkeypatch, tmp_path, model="sonnet")

    assert fake.cmd[fake.cmd.index("--effort") + 1] == "xhigh"


def test_invalid_override_raises_even_with_valid_config(configured, monkeypatch) -> None:
    configured({"agents": {"claude": {"effort": "low"}}})
    monkeypatch.setenv(EFFORT_ENV_VAR, "turbo")

    with pytest.raises(RuntimeError, match="turbo"):
        effort_args()


def test_apply_override_sets_env_for_claude(capsys, monkeypatch) -> None:
    cli._apply_effort_override("claude", "xhigh")

    assert os.environ[EFFORT_ENV_VAR] == "xhigh"
    assert capsys.readouterr().err == ""


def test_apply_override_ignores_other_agents_with_one_stderr_line(capsys, monkeypatch) -> None:
    cli._apply_effort_override("codex", "high")

    captured = capsys.readouterr()
    assert "--effort is forwarded to the claude lane only; ignored for codex" in captured.err
    assert captured.err.strip().count("\n") == 0  # exactly one line
    assert EFFORT_ENV_VAR not in os.environ


def test_apply_override_without_flag_changes_nothing(capsys) -> None:
    cli._apply_effort_override("claude", None)

    assert EFFORT_ENV_VAR not in os.environ
    assert capsys.readouterr().err == ""


def test_dispatch_rejects_invalid_effort_before_creating_detached_session(
    db, monkeypatch, tmp_path
) -> None:
    class AvailableClaude:
        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(cli, "get_handler", lambda agent: AvailableClaude())

    result = runner.invoke(
        cli.app,
        ["dispatch", "claude", "--prompt", "p", "--cwd", str(tmp_path), "--effort", "bogus"],
    )

    assert result.exit_code == 2
    assert "claude has no effort 'bogus'; valid efforts:" in result.output
    assert state.list_sessions() == []


# ---- end-to-end through the CLI ---------------------------------------------


class _AvailableHandler:
    def is_available(self) -> bool:
        return True


class _FakeDetachedPopen:
    pid = 4242

    def __init__(self, cmd, **kwargs) -> None:
        self.cmd = cmd


def test_dispatch_accepts_and_ignores_effort_for_other_agents(db, monkeypatch) -> None:
    monkeypatch.setattr("playmaker.cli.get_handler", lambda name: _AvailableHandler())
    monkeypatch.setattr("playmaker.cli.subprocess.Popen", _FakeDetachedPopen)

    result = runner.invoke(cli.app, ["dispatch", "codex", "--prompt", "hi", "--effort", "high"])

    assert result.exit_code == 0, result.output
    assert "--effort is forwarded to the claude lane only; ignored for codex" in result.stderr
    assert EFFORT_ENV_VAR not in os.environ


def test_dispatch_forwards_effort_for_claude(db, monkeypatch) -> None:
    monkeypatch.setattr("playmaker.cli.get_handler", lambda name: _AvailableHandler())
    monkeypatch.setattr("playmaker.cli.subprocess.Popen", _FakeDetachedPopen)

    result = runner.invoke(cli.app, ["dispatch", "claude", "--prompt", "hi", "--effort", "xhigh"])

    assert result.exit_code == 0, result.output
    # In-process CLI run: the override is exported for the spawned run.
    assert os.environ[EFFORT_ENV_VAR] == "xhigh"

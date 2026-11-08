"""Native Codex reasoning effort, persistence, and image attachments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.agents.codex as codex_mod
import playmaker.config as config
import playmaker.state as state
from playmaker import cli, queue
from playmaker.agents.codex import (
    CODEX_EFFORT_ENV_VAR,
    CodexHandler,
    codex_home,
    configured_model,
    resolve_effort,
    validate_model_effort,
)

runner = CliRunner()
_REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def _codex_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(CODEX_EFFORT_ENV_VAR, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(codex_mod, "CODEX_SESSIONS_ROOT", tmp_path / "sessions")


@pytest.fixture
def db(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()
    return home


class _AvailableHandler:
    def is_available(self) -> bool:
        return True


class _DetachedPopen:
    pid = 4242

    def __init__(self, cmd, **kwargs) -> None:
        self.cmd = cmd


def _detached_cli(monkeypatch) -> None:
    monkeypatch.setattr(cli, "get_handler", lambda name: _AvailableHandler())
    monkeypatch.setattr(cli.subprocess, "Popen", _DetachedPopen)


def _write_model_cache(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [
                            {"effort": value}
                            for value in ("low", "medium", "high", "xhigh", "max")
                        ],
                    },
                    {
                        "slug": "gpt-6-astra",
                        "supported_reasoning_levels": [
                            {"effort": value}
                            for value in ("low", "medium", "high", "xhigh", "max", "ultra")
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_effort_precedence(monkeypatch) -> None:
    monkeypatch.setattr(
        config, "load_config", lambda: {"agents": {"codex": {"reasoning_effort": "low"}}}
    )
    monkeypatch.setenv(CODEX_EFFORT_ENV_VAR, "medium")

    assert resolve_effort("ultra", "high") == ("ultra", "--effort")
    assert resolve_effort(None, "high") == ("high", "parent session")
    assert resolve_effort(None, None) == ("medium", CODEX_EFFORT_ENV_VAR)
    monkeypatch.delenv(CODEX_EFFORT_ENV_VAR)
    assert resolve_effort(None, "high") == ("high", "parent session")
    assert resolve_effort() == ("low", "[agents.codex] reasoning_effort")
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert resolve_effort() == (None, None)


def test_empty_codex_environment_values_mean_unset(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "  ")
    monkeypatch.setenv(CODEX_EFFORT_ENV_VAR, "  ")

    assert codex_home() == Path("~/.codex").expanduser()
    assert resolve_effort(None, "high") == ("high", "parent session")


def test_non_utf8_codex_config_has_no_configured_model(tmp_path) -> None:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True)
    (home / "config.toml").write_bytes(b"model = \xff")

    assert configured_model() is None


@pytest.mark.parametrize(
    ("source_args", "source_name"),
    [(["--effort", "bogus"], "--effort")],
)
def test_generic_rejection_exits_two_before_insert(
    db, monkeypatch, tmp_path, source_args, source_name
) -> None:
    _detached_cli(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["dispatch", "codex", "--prompt", "p", "--cwd", str(tmp_path), *source_args],
    )

    assert result.exit_code == 2
    assert "low, medium, high, xhigh, max, ultra" in result.output
    assert source_name in result.output
    assert state.list_sessions() == []


def test_env_rejection_names_its_source(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    monkeypatch.setenv(CODEX_EFFORT_ENV_VAR, "bogus")

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "p", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert CODEX_EFFORT_ENV_VAR in result.output
    assert state.list_sessions() == []


def test_config_rejection_names_its_source(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    monkeypatch.setattr(
        config, "load_config", lambda: {"agents": {"codex": {"reasoning_effort": "bogus"}}}
    )

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "p", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 2
    assert "[agents.codex] reasoning_effort" in result.output
    assert state.list_sessions() == []


def test_model_cache_rejects_pair_before_insert_and_accepts_astra_ultra(
    db, monkeypatch, tmp_path
) -> None:
    _detached_cli(monkeypatch)
    codex_home = Path(os.environ["CODEX_HOME"])
    _write_model_cache(codex_home)

    rejected = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "p",
            "--cwd",
            str(tmp_path),
            "--model",
            "gpt-5.6-luna",
            "--effort",
            "ultra",
        ],
    )
    assert rejected.exit_code == 2
    assert "gpt-5.6-luna has no reasoning effort 'ultra'" in rejected.output
    assert "valid: low, medium, high, xhigh, max" in rejected.output
    assert state.list_sessions() == []

    accepted = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "p",
            "--cwd",
            str(tmp_path),
            "--model",
            "gpt-6-astra",
            "--effort",
            "ultra",
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert state.list_sessions(limit=1)[0]["effort"] == "ultra"


@pytest.mark.parametrize("cache_body", [None, "not-json", "{}"])
def test_missing_or_malformed_cache_uses_generic_validation(tmp_path, cache_body) -> None:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    if cache_body is not None:
        (home / "models_cache.json").write_text(cache_body, encoding="utf-8")

    validate_model_effort("gpt-5.6-luna", "ultra")


@pytest.mark.parametrize(
    "levels",
    [["low", "ultra"], [{"name": "ultra"}], []],
)
def test_malformed_reasoning_levels_use_generic_validation(levels) -> None:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.6-luna", "supported_reasoning_levels": levels}
                ]
            }
        ),
        encoding="utf-8",
    )

    validate_model_effort("gpt-5.6-luna", "ultra")


class _Stream:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def read(self) -> str:
        return "".join(self.lines)


class _RecordingPopen:
    def __init__(self) -> None:
        self.cmds: list[list[str]] = []
        self.returncode = 0
        self.stdout = _Stream([])
        self.stderr = _Stream([])

    def __call__(self, cmd, **kwargs):
        if "-o" not in cmd:
            return _REAL_POPEN(cmd, **kwargs)
        self.cmds.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("answer", encoding="utf-8")
        self.stdout = _Stream(['{"type":"thread.started","thread_id":"thread-1"}\n'])
        self.stderr = _Stream([])
        return self

    def wait(self) -> int:
        return self.returncode


@pytest.mark.parametrize("effort", [None, "ultra"])
@pytest.mark.parametrize("with_image", [False, True])
def test_exec_and_resume_argv(monkeypatch, tmp_path, effort, with_image) -> None:
    fake = _RecordingPopen()
    monkeypatch.setattr(codex_mod.subprocess, "Popen", fake)
    image = tmp_path / "ScreenShot.PNG"
    image.write_bytes(b"png")
    files = [Path("notes.md"), image] if with_image else []
    handler = CodexHandler()

    handler.dispatch("prompt", tmp_path, files=files, effort=effort)
    handler.resume("prompt", tmp_path, "thread-1", files=files, effort=effort)

    dispatch_cmd, resume_cmd = fake.cmds
    for cmd in (dispatch_cmd, resume_cmd):
        assert cmd[-1] == ("prompt\n\n@notes.md" if with_image else "prompt")
        image_args = [arg for arg in cmd if arg.startswith("--image=")]
        assert image_args == ([f"--image={image.resolve()}"] if with_image else [])
        assert str(image) not in cmd[-1]
        if effort:
            index = cmd.index("-c")
            assert cmd[index + 1] == 'model_reasoning_effort="ultra"'
        else:
            assert "-c" not in cmd
    assert resume_cmd[-2] == "thread-1"

    if effort is None and not with_image:
        dispatch_out = dispatch_cmd[dispatch_cmd.index("-o") + 1]
        resume_out = resume_cmd[resume_cmd.index("-o") + 1]
        assert dispatch_cmd == [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--cd",
            str(tmp_path),
            "-o",
            dispatch_out,
            "prompt",
        ]
        assert resume_cmd == [
            "codex",
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "-o",
            resume_out,
            "thread-1",
            "prompt",
        ]


def test_image_symlink_keeps_given_suffix_and_path(monkeypatch, tmp_path) -> None:
    fake = _RecordingPopen()
    monkeypatch.setattr(codex_mod.subprocess, "Popen", fake)
    target = tmp_path / "blob_1234"
    target.write_bytes(b"png")
    image = tmp_path / "shot.png"
    image.symlink_to(target)

    CodexHandler().dispatch("prompt", tmp_path, files=[image])

    command = fake.cmds[0]
    assert f"--image={os.path.abspath(image)}" in command
    assert str(target) not in command[-1]


def test_missing_image_refused_before_insert(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    missing = tmp_path / "missing.webp"

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "p",
            "--cwd",
            str(tmp_path),
            "--files",
            str(missing),
        ],
    )

    assert result.exit_code == 2
    assert str(missing) in result.output
    assert state.list_sessions() == []


def test_continue_inherits_parent_effort_before_config(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    monkeypatch.setattr(
        config, "load_config", lambda: {"agents": {"codex": {"reasoning_effort": "low"}}}
    )
    sid = state.insert_session(
        agent="codex", prompt="first", cwd=str(tmp_path), model="gpt-6-astra", effort="ultra"
    )
    state.update_session(sid, status="done", agent_session_id="thread-1")

    result = runner.invoke(cli.app, ["continue", sid, "--prompt", "next"])

    assert result.exit_code == 0, result.output
    child = state.list_sessions(limit=1)[0]
    assert child["parent_id"] == sid
    assert child["effort"] == "ultra"


def test_continue_prefers_parent_effort_over_env(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    monkeypatch.setenv(CODEX_EFFORT_ENV_VAR, "medium")
    sid = state.insert_session(
        agent="codex", prompt="first", cwd=str(tmp_path), model="gpt-6-astra", effort="ultra"
    )
    state.update_session(sid, status="done", agent_session_id="thread-1")

    result = runner.invoke(cli.app, ["continue", sid, "--prompt", "next"])

    assert result.exit_code == 0, result.output
    child = state.list_sessions(limit=1)[0]
    assert child["parent_id"] == sid
    assert child["effort"] == "ultra"


def test_continue_effort_override_beats_parent_and_env(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    monkeypatch.setenv(CODEX_EFFORT_ENV_VAR, "medium")
    sid = state.insert_session(
        agent="codex", prompt="first", cwd=str(tmp_path), model="gpt-6-astra", effort="ultra"
    )
    state.update_session(sid, status="done", agent_session_id="thread-1")

    result = runner.invoke(cli.app, ["continue", sid, "--prompt", "next", "--effort", "low"])

    assert result.exit_code == 0, result.output
    child = state.list_sessions(limit=1)[0]
    assert child["parent_id"] == sid
    assert child["effort"] == "low"


def test_continue_falls_back_to_env_without_parent_effort(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    monkeypatch.setenv(CODEX_EFFORT_ENV_VAR, "high")
    sid = state.insert_session(agent="codex", prompt="first", cwd=str(tmp_path))
    state.update_session(sid, status="done", agent_session_id="thread-1")

    result = runner.invoke(cli.app, ["continue", sid, "--prompt", "next"])

    assert result.exit_code == 0, result.output
    child = state.list_sessions(limit=1)[0]
    assert child["parent_id"] == sid
    assert child["effort"] == "high"


def test_queued_continue_keeps_effort_through_resume_argv(db, monkeypatch, tmp_path) -> None:
    _detached_cli(monkeypatch)
    root = state.insert_session(
        agent="codex", prompt="first", cwd=str(tmp_path), effort="ultra"
    )
    state.update_session(root, status="done", agent_session_id="thread-1")

    queued = runner.invoke(cli.app, ["continue", root, "--prompt", "fix", "--after"])
    assert queued.exit_code == 0, queued.output
    assert queue.list_items(root)[0]["effort"] == "ultra"

    child = queue.deliver_next(root, root, lambda sid: 4242)
    assert child is not None
    assert state.get_session(child)["effort"] == "ultra"

    fake = _RecordingPopen()
    monkeypatch.setattr(codex_mod.subprocess, "Popen", fake)
    monkeypatch.setattr(cli, "get_handler", lambda name: CodexHandler())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    cli._run_dispatch(child)

    command = fake.cmds[0]
    assert command[:3] == ["codex", "exec", "resume"]
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="ultra"'


def test_empty_model_uses_effective_model_for_dispatch_and_continue(
    db, monkeypatch, tmp_path
) -> None:
    _detached_cli(monkeypatch)
    home = Path(os.environ["CODEX_HOME"])
    _write_model_cache(home)
    (home / "config.toml").write_text('model = "gpt-5.6-luna"\n', encoding="utf-8")

    dispatched = runner.invoke(
        cli.app,
        [
            "dispatch",
            "codex",
            "--prompt",
            "p",
            "--model",
            "  ",
            "--effort",
            "ultra",
        ],
    )
    assert dispatched.exit_code == 2
    assert state.list_sessions() == []

    parent = state.insert_session(
        agent="codex", prompt="p", cwd=str(tmp_path), model="gpt-5.6-luna"
    )
    state.update_session(parent, status="done", agent_session_id="thread-1")
    continued = runner.invoke(
        cli.app,
        ["continue", parent, "--prompt", "p", "--model", "  ", "--effort", "ultra"],
    )
    assert continued.exit_code == 2
    assert len(state.list_sessions()) == 1


def test_get_text_and_json_show_effort(db) -> None:
    sid = state.insert_session(agent="codex", prompt="p", cwd="/repo", effort="ultra")

    text_result = runner.invoke(cli.app, ["get", sid])
    json_result = runner.invoke(cli.app, ["get", sid, "--json"])

    assert "effort:   ultra" in text_result.output
    assert json.loads(json_result.output)["effort"] == "ultra"


def test_fallback_does_not_carry_codex_effort(db, monkeypatch, tmp_path) -> None:
    sid = state.insert_session(
        agent="codex", prompt="p", cwd=str(tmp_path), effort="ultra", batch_id="fb"
    )
    state.update_session(sid, fallback_lanes=json.dumps(["claude"]), status="running")
    row = state.get_session(sid)
    spawned: list[str] = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda child: spawned.append(child) or 4242)
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    cli._maybe_fallback(sid, row, "quota")

    assert len(spawned) == 1
    replacement = state.get_session(spawned[0])
    assert replacement["agent"] == "claude"
    assert replacement["effort"] is None


def test_fallback_to_codex_resolves_own_effort_and_survives_invalid_value(
    db, monkeypatch, tmp_path, capsys
) -> None:
    spawned: list[str] = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda child: spawned.append(child) or 4242)
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        config, "load_config", lambda: {"agents": {"codex": {"reasoning_effort": "high"}}}
    )
    first = state.insert_session(agent="claude", prompt="p", cwd=str(tmp_path))
    state.update_session(first, fallback_lanes=json.dumps(["codex"]), status="running")

    cli._maybe_fallback(first, state.get_session(first), "quota")

    assert state.get_session(spawned[-1])["effort"] == "high"

    monkeypatch.setattr(
        config, "load_config", lambda: {"agents": {"codex": {"reasoning_effort": "bogus"}}}
    )
    second = state.insert_session(agent="claude", prompt="p", cwd=str(tmp_path))
    state.update_session(second, fallback_lanes=json.dumps(["codex"]), status="running")

    cli._maybe_fallback(second, state.get_session(second), "quota")

    assert state.get_session(spawned[-1])["effort"] is None
    assert "codex fallback continuing without effort" in capsys.readouterr().err

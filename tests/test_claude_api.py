"""The claude-api lane: the Claude Agent SDK on an API key, and its local credit counter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
import playmaker.events as events
import playmaker.quotas as quotas
import playmaker.registry as registry
import playmaker.state as state
from playmaker import cli
from playmaker.agents import claude as claude_mod
from playmaker.agents import claude_sdk
from playmaker.agents.base import DispatchResult
from playmaker.agents.claude import (
    API_KEY_ENV_VAR,
    EFFORT_ENV_VAR,
    KEYCHAIN_ADD_COMMAND,
    ClaudeHandler,
    effort_args,
    load_api_key,
    permission_args,
)
from playmaker.agents.claude_sdk import RUNNER_PATH, SDK_PACKAGE, SDK_VERSION, ClaudeSdkHandler
from playmaker.config import agent_binary

runner = CliRunner()

# A stand-in that must never show up in a command line, a message or the CLI output.
KEY = "sk-ant-TEST-not-a-real-key-never-printed"


@pytest.fixture
def configured(monkeypatch):
    """Install a fake config.toml body for the duration of a test."""

    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The handler reads real os.environ — keep the developer's shell out."""
    monkeypatch.delenv(EFFORT_ENV_VAR, raising=False)
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


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


class _FakeStdin:
    def __init__(self) -> None:
        self.written = ""
        self.closed = False

    def write(self, text: str) -> int:
        self.written += text
        return len(text)

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    """Stand-in for subprocess.Popen that records the command, its kwargs and its stdin."""

    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self._lines = lines
        self.returncode = returncode
        self._stderr = stderr
        self.cmd: list[str] = []
        self.kwargs: dict = {}
        self.stdin: _FakeStdin | None = None
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdin = _FakeStdin() if kwargs.get("stdin") is not None else None
        self.stdout = _FakeStream(self._lines)
        self.stderr = _FakeStream([self._stderr])
        return self

    def wait(self) -> int:
        return self.returncode


def _success_lines(cost: float = 0.0123) -> list[str]:
    return [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-api"}) + "\n",
        json.dumps(
            {"type": "result", "subtype": "success", "result": "ok", "total_cost_usd": cost}
        )
        + "\n",
    ]


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def keychain(monkeypatch):
    """A fake `security`: the existence check, then the `-w` read. Returns the calls."""

    def _install(secret: str | None) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(claude_mod.sys, "platform", "darwin")
        monkeypatch.setattr(claude_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "-w" not in cmd:
                if secret is None:
                    return _Completed(returncode=44, stderr="could not be found")
                return _Completed(returncode=0, stdout="keychain: ...")
            return _Completed(returncode=0, stdout=secret + "\n")

        monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)
        return calls

    return _install


def _with_key(monkeypatch) -> None:
    monkeypatch.setattr(claude_mod, "load_api_key", lambda agent: KEY)
    monkeypatch.setattr(claude_sdk, "load_api_key", lambda agent: KEY)


def _install_fake_sdk(home: Path, version: str = SDK_VERSION) -> Path:
    """An sdk-runner directory holding only the SDK's package.json at `version`."""
    package = home / "sdk-runner" / "node_modules" / SDK_PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        json.dumps({"name": SDK_PACKAGE, "version": version}), encoding="utf-8"
    )
    return home / "sdk-runner"


@pytest.fixture
def sdk_ready(db, monkeypatch) -> Path:
    """Node 24 on PATH and the pinned SDK in the disposable home; returns the SDK dir."""
    monkeypatch.setattr(claude_sdk.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(claude_sdk, "node_version", lambda executable: (24, 15, 0))
    return _install_fake_sdk(db)


# ---- the lane itself --------------------------------------------------------


def test_registry_has_claude_api_as_the_sdk_claude_lane(configured) -> None:
    configured({})
    handler = registry.get_handler("claude-api")

    assert isinstance(handler, ClaudeSdkHandler)
    assert isinstance(handler, ClaudeHandler)  # parsing, resume and --effort are shared
    assert handler.name == "claude-api"
    assert handler.prompt_on_stdin
    assert not handler.bare
    assert not registry.get_handler("claude").prompt_on_stdin
    assert registry.claude_lanes() == ("claude", "claude-api")


def test_an_unconfigured_claude_api_lane_runs_node(configured) -> None:
    # The lane name is not an executable: the runner runs on Node.
    configured({})

    assert agent_binary("claude-api") == "node"
    assert agent_binary("claude") == "claude"


def test_settings_come_from_the_lanes_own_section(configured) -> None:
    configured(
        {
            "agents": {
                "claude": {"yolo": True, "effort": "xhigh"},
                "claude-api": {"effort": "low"},
            }
        }
    )

    assert permission_args("claude") == ["--dangerously-skip-permissions"]
    assert permission_args("claude-api") == ["--permission-mode", "acceptEdits"]
    assert effort_args("claude") == ["--effort", "xhigh"]
    assert effort_args("claude-api") == ["--effort", "low"]


def test_an_invalid_effort_names_the_lanes_section(configured) -> None:
    configured({"agents": {"claude-api": {"effort": "ultra"}}})

    with pytest.raises(RuntimeError, match=re.escape("[agents.claude-api] effort")):
        effort_args("claude-api")


def test_bare_in_config_overrides_the_lane_default(configured) -> None:
    configured({"agents": {"claude": {"bare": True}, "claude-api": {"bare": False}}})

    assert ClaudeHandler().bare
    assert not ClaudeHandler("claude-api", bare=True).bare


def test_a_leftover_bare_setting_no_longer_applies_to_the_sdk_lane(configured) -> None:
    # A config.toml written for the `claude --bare` lane still says `bare = true`.
    configured({"agents": {"claude-api": {"bare": True}}})

    assert not ClaudeSdkHandler().bare


# ---- command shape and environment ------------------------------------------


def test_dispatch_runs_the_sdk_runner_with_the_prompt_on_stdin(
    db, configured, monkeypatch, tmp_path
) -> None:
    configured({"agents": {"claude-api": {"effort": "high", "allowed_tools": ["Read", "Edit"]}}})
    _with_key(monkeypatch)
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    result = ClaudeSdkHandler().dispatch(prompt="do the thing", cwd=tmp_path, model="haiku")

    assert fake.cmd[:4] == ["node", str(RUNNER_PATH), "--sdk-dir", str(db / "sdk-runner")]
    assert fake.cmd[4:] == [
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Edit",
        "--model",
        "haiku",
        "--effort",
        "high",
    ]
    assert "do the thing" not in fake.cmd  # the prompt travels on stdin
    assert fake.stdin is not None
    assert (fake.stdin.written, fake.stdin.closed) == ("do the thing", True)
    assert fake.kwargs["cwd"] == str(tmp_path)
    assert result.agent_session_id == "sess-api"
    assert result.cost_usd == 0.0123


def test_the_worker_gets_the_key_and_none_of_the_launching_sessions_variables(
    configured, monkeypatch, tmp_path
) -> None:
    # A dispatch from inside Claude Code inherits that session's entrypoint, auth
    # hand-off and base URL; the SDK keeps an inherited entrypoint as is.
    configured({})
    _with_key(monkeypatch)
    inherited = {
        "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
        "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1",
        "CLAUDE_AGENT_SDK_VERSION": "0.3.309",
        "CLAUDECODE": "1",
        "CLAUDE_CONFIG_DIR": "/elsewhere",
        "ANTHROPIC_BASE_URL": "https://proxy.example",
        "ANTHROPIC_AUTH_TOKEN": "bearer",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PLAYMAKER_TEST_KEEP", "kept")
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    ClaudeSdkHandler().dispatch(prompt="p", cwd=tmp_path, model="haiku")

    env = fake.kwargs["env"]
    assert env[API_KEY_ENV_VAR] == KEY
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_AGENT_SDK_CLIENT_APP"].startswith("playmaker/")
    assert not set(inherited) & set(env)
    assert env["PLAYMAKER_TEST_KEEP"] == "kept"
    assert env["PATH"] == os.environ["PATH"]
    assert KEY not in " ".join(fake.cmd)
    assert API_KEY_ENV_VAR not in os.environ  # the parent process is untouched
    assert os.environ["CLAUDE_CODE_ENTRYPOINT"] == "claude-desktop"


def test_max_turns_reaches_the_runner(db, configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude-api": {"max_turns": 40}}})
    _with_key(monkeypatch)
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    ClaudeSdkHandler().dispatch(prompt="p", cwd=tmp_path, model="haiku")

    assert fake.cmd[4:6] == ["--max-turns", "40"]


def test_yolo_reaches_the_runner_as_the_cli_flag(db, configured, monkeypatch, tmp_path) -> None:
    configured({"agents": {"claude-api": {"yolo": True}}})
    _with_key(monkeypatch)
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    ClaudeSdkHandler().dispatch(prompt="p", cwd=tmp_path, model="haiku")

    assert "--dangerously-skip-permissions" in fake.cmd
    assert "--permission-mode" not in fake.cmd


def test_the_subscription_lane_sends_no_bare_flag_and_inherits_its_environment(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "--bare" not in fake.cmd
    assert fake.kwargs["env"] is None


def test_resume_runs_the_runner_with_resume_and_the_prompt_on_stdin(
    db, configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _with_key(monkeypatch)
    recorded: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        return _Completed(
            stdout=json.dumps(
                {
                    "type": "result",
                    "session_id": "sess-api",
                    "result": "ok",
                    "total_cost_usd": 0.004,
                    "duration_ms": 10,
                }
            )
        )

    monkeypatch.setattr("playmaker.agents.claude.subprocess.run", fake_run)

    result = ClaudeSdkHandler().resume(
        prompt="more", cwd=tmp_path, agent_session_id="sess-api", model="haiku"
    )

    ((cmd, kwargs),) = recorded
    assert cmd[:2] == ["node", str(RUNNER_PATH)]
    assert cmd[4:8] == ["--resume", "sess-api", "--output-format", "json"]
    assert "more" not in cmd
    assert kwargs["input"] == "more"
    assert kwargs["env"][API_KEY_ENV_VAR] == KEY
    assert KEY not in " ".join(cmd)
    assert (result.agent_session_id, result.cost_usd) == ("sess-api", 0.004)


def test_the_subscription_lane_still_takes_the_prompt_on_the_command_line(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert fake.cmd[:2] == ["claude", "-p"]
    assert fake.cmd[-1] == "p"
    assert fake.kwargs["stdin"] is None


def test_a_missing_key_refuses_before_any_process_starts(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    monkeypatch.setattr(claude_mod.sys, "platform", "darwin")
    monkeypatch.setattr(claude_mod, "_keychain_api_key", lambda: None)
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match=re.escape(KEYCHAIN_ADD_COMMAND)):
        ClaudeSdkHandler().dispatch(prompt="p", cwd=tmp_path)

    assert fake.cmd == []


def test_a_failure_message_names_the_lane_and_carries_no_key(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _with_key(monkeypatch)
    fake = _FakePopen(_success_lines()[:1], returncode=1, stderr="Invalid API key")
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="claude-api failed") as info:
        ClaudeSdkHandler().dispatch(prompt="p", cwd=tmp_path)

    assert KEY not in str(info.value)


def test_an_empty_balance_reads_as_a_quota_refusal(configured, monkeypatch, tmp_path) -> None:
    configured({})
    _with_key(monkeypatch)
    error = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "Credit balance is too low",
            "session_id": "sess-api",
            "total_cost_usd": 0,
        }
    )
    fake = _FakePopen(
        [_success_lines()[0], error + "\n"], returncode=1, stderr="Credit balance is too low\n"
    )
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError) as info:
        ClaudeSdkHandler().dispatch(prompt="p", cwd=tmp_path, model="haiku")

    assert str(info.value) == "claude-api failed (exit 1): Credit balance is too low"
    assert events.classify_failure(str(info.value)) == "quota"


# ---- preflight: Node, the SDK, max_turns ------------------------------------


def test_preflight_passes_with_node_the_pinned_sdk_and_a_key(
    sdk_ready, configured, monkeypatch
) -> None:
    configured({})
    _with_key(monkeypatch)

    ClaudeSdkHandler().preflight()


def test_preflight_without_node_names_the_binary_setting(db, configured, monkeypatch) -> None:
    configured({})
    monkeypatch.setattr(claude_sdk.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match=r"Node not found \('node'\)") as info:
        ClaudeSdkHandler().preflight()

    assert "[agents.claude-api] binary" in str(info.value)


def test_preflight_refuses_a_binary_that_is_not_node(db, configured, monkeypatch) -> None:
    # A config.toml written for the `claude --bare` lane still says binary = "claude".
    configured({"agents": {"claude-api": {"binary": "claude"}}})
    monkeypatch.setattr(claude_sdk.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(claude_sdk, "node_version", lambda executable: None)

    with pytest.raises(RuntimeError, match="'claude' is not Node") as info:
        ClaudeSdkHandler().preflight()

    assert "no longer runs `claude --bare`" in str(info.value)


def test_preflight_refuses_a_node_older_than_the_sdk_needs(db, configured, monkeypatch) -> None:
    configured({})
    monkeypatch.setattr(claude_sdk.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(claude_sdk, "node_version", lambda executable: (16, 13, 0))

    with pytest.raises(RuntimeError, match=r"Node 16\.13\.0 is too old .*\(needs 18\.14\+\)"):
        ClaudeSdkHandler().preflight()


def test_preflight_without_the_sdk_says_how_to_install_it(db, configured, monkeypatch) -> None:
    configured({})
    monkeypatch.setattr(claude_sdk.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(claude_sdk, "node_version", lambda executable: (24, 15, 0))

    with pytest.raises(RuntimeError, match="not installed in .*sdk-runner — run: playmaker sdk"):
        ClaudeSdkHandler().preflight()


def test_preflight_refuses_an_sdk_other_than_the_pinned_one(db, configured, monkeypatch) -> None:
    configured({})
    monkeypatch.setattr(claude_sdk.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(claude_sdk, "node_version", lambda executable: (24, 15, 0))
    _install_fake_sdk(db, version="0.3.1")

    with pytest.raises(RuntimeError, match=f"holds {SDK_PACKAGE}@0.3.1, .* pins {SDK_VERSION}"):
        ClaudeSdkHandler().preflight()


@pytest.mark.parametrize("value", [0, -3, "40", True])
def test_preflight_refuses_a_bad_max_turns(sdk_ready, configured, value) -> None:
    configured({"agents": {"claude-api": {"max_turns": value}}})

    with pytest.raises(RuntimeError, match=r"\[agents.claude-api\] max_turns"):
        ClaudeSdkHandler().preflight()


def test_node_version_reads_vxyz_and_rejects_anything_else(monkeypatch) -> None:
    replies = {"node": "v24.15.0\n", "claude": "2.1.312 (Claude Code)\n"}

    def fake_run(cmd, **kwargs):
        return _Completed(stdout=replies[cmd[0]])

    monkeypatch.setattr(claude_sdk.subprocess, "run", fake_run)

    assert claude_sdk.node_version("node") == (24, 15, 0)
    assert claude_sdk.node_version("claude") is None


# ---- key sources ------------------------------------------------------------


def test_the_keychain_source_reads_the_item_in_two_security_calls(configured, keychain) -> None:
    configured({})  # key_source defaults to the keychain
    calls = keychain(KEY)

    assert load_api_key("claude-api") == KEY
    assert calls == [
        ["security", "find-generic-password", "-s", "playmaker-anthropic-api"],
        ["security", "find-generic-password", "-s", "playmaker-anthropic-api", "-w"],
    ]


def test_an_empty_keychain_names_the_add_command_and_the_file_alternative(
    configured, keychain
) -> None:
    configured({})
    keychain(None)

    with pytest.raises(RuntimeError) as info:
        load_api_key("claude-api")

    message = str(info.value)
    assert KEYCHAIN_ADD_COMMAND in message
    assert 'key_source = "file"' in message


def test_the_keychain_is_never_consulted_off_macos(configured, monkeypatch) -> None:
    configured({})
    monkeypatch.setattr(claude_mod.sys, "platform", "linux")

    def forbidden(*args, **kwargs):
        raise AssertionError("security must not run on linux")

    monkeypatch.setattr(claude_mod.subprocess, "run", forbidden)

    with pytest.raises(RuntimeError, match="macOS-only"):
        load_api_key("claude-api")


def test_the_file_source_reads_secrets_env_with_mode_600(db, configured) -> None:
    configured({"agents": {"claude-api": {"key_source": "file"}}})
    secrets = db / "secrets.env"
    secrets.write_text(f"# console key\nexport {API_KEY_ENV_VAR}='{KEY}'\n", encoding="utf-8")
    secrets.chmod(0o600)

    assert load_api_key("claude-api") == KEY


def test_the_file_source_refuses_a_file_others_can_read(db, configured) -> None:
    configured({"agents": {"claude-api": {"key_source": "file"}}})
    secrets = db / "secrets.env"
    secrets.write_text(f"{API_KEY_ENV_VAR}={KEY}\n", encoding="utf-8")
    secrets.chmod(0o644)

    with pytest.raises(RuntimeError, match="chmod 600") as info:
        load_api_key("claude-api")

    assert KEY not in str(info.value)


def test_the_file_source_without_a_file_says_how_to_create_it(db, configured) -> None:
    configured({"agents": {"claude-api": {"key_source": "file"}}})

    with pytest.raises(RuntimeError, match="secrets.env"):
        load_api_key("claude-api")


def test_an_unknown_key_source_is_refused(configured) -> None:
    configured({"agents": {"claude-api": {"key_source": "env"}}})

    with pytest.raises(RuntimeError, match="valid sources: keychain, file"):
        load_api_key("claude-api")


# ---- the CLI ----------------------------------------------------------------


def _claude_on_path(monkeypatch) -> None:
    monkeypatch.setattr(claude_mod.shutil, "which", lambda name: f"/usr/local/bin/{name}")


def test_dispatch_without_a_key_exits_2_before_any_session_row(
    sdk_ready, configured, monkeypatch, tmp_path
) -> None:
    configured({})
    monkeypatch.setattr(claude_mod.sys, "platform", "darwin")
    monkeypatch.setattr(claude_mod, "_keychain_api_key", lambda: None)

    result = runner.invoke(
        cli.app, ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 2, result.output
    assert "no API key in the macOS Keychain" in result.output
    assert "security add-generic-password -s playmaker-anthropic-api" in result.output
    assert state.list_sessions() == []


def test_dispatch_without_the_sdk_exits_2_before_any_session_row(
    db, configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _claude_on_path(monkeypatch)
    monkeypatch.setattr(claude_sdk, "node_version", lambda executable: (24, 15, 0))
    _with_key(monkeypatch)

    result = runner.invoke(
        cli.app, ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 2, result.output
    assert "run: playmaker sdk install" in result.output
    assert state.list_sessions() == []


def test_a_sync_dispatch_records_the_cost_and_never_prints_the_key(
    sdk_ready, configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _with_key(monkeypatch)
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    fake = _FakePopen(_success_lines(cost=0.0123))
    # Only the handler's Popen is faked: the CLI's own git snapshot still runs
    # real subprocesses, and a global Popen patch would break those.
    monkeypatch.setattr(
        claude_mod, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": fake})
    )

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "claude-api",
            "--model",
            "haiku",
            "--sync",
            "--read-only",
            "--cwd",
            str(tmp_path),
            "--prompt",
            "answer in one word: ok",
        ],
    )

    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    assert fake.cmd[1] == str(RUNNER_PATH)
    assert "--bare" not in fake.cmd
    assert fake.stdin is not None and fake.stdin.written == "answer in one word: ok"
    assert fake.kwargs["env"][API_KEY_ENV_VAR] == KEY
    (row,) = state.list_sessions()
    assert row["agent"] == "claude-api"
    assert row["cost_usd"] == 0.0123


def _fake_node(path: Path, body: str) -> Path:
    """An executable standing in for `node`: `--version` says v24, anything else runs `body`."""
    path.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """\
            import json, sys
            if sys.argv[1:] == ["--version"]:
                print("v24.15.0")
                sys.exit(0)
            prompt = sys.stdin.read()
            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_an_empty_balance_through_a_stand_in_runner_fails_as_quota(
    db, configured, monkeypatch, tmp_path
) -> None:
    # End to end through real processes: dispatch → preflight → the "node" the
    # config names → the failure classifier. The stand-in prints what the runner
    # prints when Console answers «Credit balance is too low».
    node = _fake_node(
        tmp_path / "node",
        """\
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-api",
                          "apiKeySource": "ANTHROPIC_API_KEY"}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Credit balance is too low", "session_id": "sess-api",
                          "total_cost_usd": 0}), flush=True)
        sys.stderr.write("Credit balance is too low\\n")
        sys.exit(1)
        """,
    )
    configured({"agents": {"claude-api": {"binary": str(node)}}})
    _install_fake_sdk(db)
    _with_key(monkeypatch)
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(
        cli.app,
        ["dispatch", "claude-api", "--model", "haiku", "--sync", "--cwd", str(repo),
         "--prompt", "Reply with the single word OK"],
    )

    assert result.exit_code == 1, result.output
    assert "Credit balance is too low" in result.output
    assert KEY not in result.output
    (row,) = state.list_sessions()
    assert (row["agent"], row["status"], row["fail_reason"]) == ("claude-api", "failed", "quota")
    assert row["agent_session_id"] == "sess-api"


def test_a_successful_stand_in_runner_run_is_done_with_its_cost(
    db, configured, monkeypatch, tmp_path
) -> None:
    node = _fake_node(
        tmp_path / "node",
        """\
        assert "--sdk-dir" in sys.argv and "--model" in sys.argv, sys.argv
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-ok",
                          "apiKeySource": "ANTHROPIC_API_KEY"}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": prompt.upper(), "session_id": "sess-ok",
                          "total_cost_usd": 0.0081, "duration_ms": 1200, "num_turns": 1}))
        """,
    )
    configured({"agents": {"claude-api": {"binary": str(node)}}})
    _install_fake_sdk(db)
    _with_key(monkeypatch)
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(
        cli.app,
        ["dispatch", "claude-api", "--model", "haiku", "--sync", "--read-only",
         "--cwd", str(repo), "--prompt", "ok"],
    )

    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    (row,) = state.list_sessions()
    assert (row["status"], row["cost_usd"], row["duration_seconds"]) == ("done", 0.0081, 1.2)


def test_effort_is_forwarded_to_the_api_lane(capsys) -> None:
    cli._apply_effort_override("claude-api", "xhigh")

    assert os.environ[EFFORT_ENV_VAR] == "xhigh"
    assert capsys.readouterr().err == ""


def test_an_invalid_effort_on_the_api_lane_names_its_section(db, monkeypatch, tmp_path) -> None:
    class AvailableLane:
        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(cli, "get_handler", lambda agent: AvailableLane())

    result = runner.invoke(
        cli.app,
        ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(tmp_path), "--effort", "bogus"],
    )

    assert result.exit_code == 2
    assert "claude-api has no effort 'bogus'" in result.output
    assert "[agents.claude-api] effort" in result.output
    assert state.list_sessions() == []


# ---- the credit counter -----------------------------------------------------


@pytest.mark.parametrize(
    ("today", "reset_day", "start", "next_reset"),
    [
        (date(2026, 10, 20), 4, date(2026, 10, 4), date(2026, 11, 4)),
        (date(2026, 10, 4), 4, date(2026, 10, 4), date(2026, 11, 4)),
        (date(2026, 10, 3), 4, date(2026, 9, 4), date(2026, 10, 4)),
        (date(2026, 1, 2), 4, date(2025, 12, 4), date(2026, 1, 4)),
        (date(2026, 12, 25), 4, date(2026, 12, 4), date(2027, 1, 4)),
        (date(2026, 2, 10), 31, date(2026, 1, 31), date(2026, 2, 28)),
        (date(2026, 3, 1), 31, date(2026, 2, 28), date(2026, 3, 31)),
        (date(2026, 10, 20), 0, date(2026, 10, 1), date(2026, 11, 1)),
    ],
)
def test_cycle_bounds(today: date, reset_day: int, start: date, next_reset: date) -> None:
    assert quotas.credit_cycle_bounds(today, reset_day) == (start, next_reset)


def _seed(agent: str, cost: float | None, started: datetime, status: str = "done") -> str:
    sid = state.insert_session(agent=agent, prompt="p", cwd="/repo")
    with state.connect() as conn:
        conn.execute(
            "UPDATE sessions SET cost_usd = ?, started_at = ?, status = ? WHERE id = ?",
            (cost, started.isoformat(), status, sid),
        )
        conn.commit()
    return sid


_API = {"credit_reset_day": 4, "monthly_cap_usd": 200, "stop_at_usd": 200, "warn_below_usd": 15}


def test_the_counter_sums_only_this_lane_since_the_reset_day(db, configured) -> None:
    configured({"agents": {"claude-api": _API}})
    _seed("claude-api", 1.25, datetime(2026, 10, 5, 12, tzinfo=UTC))
    _seed("claude-api", 2.0, datetime(2026, 10, 20, 9, tzinfo=UTC))
    _seed("claude-api", None, datetime(2026, 10, 19, tzinfo=UTC), status="failed")
    _seed("claude-api", 50.0, datetime(2026, 10, 3, 23, 59, tzinfo=UTC))  # last cycle
    _seed("claude", 100.0, datetime(2026, 10, 10, tzinfo=UTC))  # the subscription lane

    cycle = quotas.credit_cycle("claude-api", today=date(2026, 10, 20))

    assert cycle.spent_usd == pytest.approx(3.25)
    assert cycle.sessions == 3
    assert (cycle.cycle_start, cycle.next_reset) == (date(2026, 10, 4), date(2026, 11, 4))
    assert (cycle.cap_usd, cycle.stop_at_usd, cycle.warn_below_usd) == (200.0, 200.0, 15.0)
    assert cycle.left_usd == pytest.approx(196.75)
    assert cycle.pct_left == 98
    assert not cycle.stop_reached
    assert not cycle.low


def test_the_probe_reports_spent_of_cap_since_the_reset_day(db, configured) -> None:
    configured({"agents": {"claude-api": _API}})
    now = datetime.now(UTC)
    _seed("claude-api", 3.25, now)
    start, next_reset = quotas.credit_cycle_bounds(now.date(), 4)

    probe = quotas.claude_api_probe()

    assert probe["status"] == "ok"
    assert probe["note"].startswith(
        f"spent $3.25 of $200 since {start.isoformat()}, $196.75 left"
    )
    assert "stop at $200" in probe["note"]
    assert "low" not in probe["note"]
    assert probe["low"] is False
    assert probe["next_reset"] == next_reset.isoformat()
    (window,) = probe["windows"]
    assert window["name"] == "Credits"
    assert window["pct_left"] == 98


def test_the_probe_without_a_cap_reports_the_spend_alone(db, configured) -> None:
    configured({})
    _seed("claude-api", 0.5, datetime.now(UTC))

    probe = quotas.claude_api_probe()

    assert probe["note"].startswith("spent $0.50 of an unknown cap since ")
    assert probe["windows"] == []
    assert probe["stop_at_usd"] is None
    assert probe["low"] is False


def test_the_probe_marks_a_low_balance_under_the_warn_line(db, configured) -> None:
    configured({"agents": {"claude-api": _API}})
    _seed("claude-api", 190.0, datetime.now(UTC))

    probe = quotas.claude_api_probe()

    assert "$10.00 left — low (below warn_below_usd=$15)" in probe["note"]
    assert probe["low"] is True
    assert "reached" not in probe["note"]  # low is a mark, the stop line is $200


def test_the_probe_marks_a_reached_stop_line(db, configured) -> None:
    configured({"agents": {"claude-api": {**_API, "stop_at_usd": 3}}})
    _seed("claude-api", 3.0, datetime.now(UTC))

    probe = quotas.claude_api_probe()

    assert "stop at $3 — reached, dispatch refuses (--force overrides)" in probe["note"]


def test_the_quotas_command_renders_the_claude_api_block(db, configured, monkeypatch) -> None:
    configured({"agents": {"claude-api": _API}})
    _seed("claude-api", 12.5, datetime.now(UTC))
    snapshot = db / "quotas.json"
    monkeypatch.setattr(state, "QUOTAS_PATH", snapshot)
    snapshot.write_text(
        json.dumps(
            {
                "fetched_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
                "providers": {"claude-api": quotas.claude_api_probe()},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["quotas", "--cached"])

    assert result.exit_code == 0, result.output
    assert "Claude API" in result.output
    assert "spent $12.50 of $200 since" in result.output
    assert "$187.50 left" in result.output
    assert "Credits" in result.output


def test_refresh_all_includes_the_local_probe(db, configured, monkeypatch) -> None:
    configured({"agents": {"claude-api": _API}})
    for name in list(quotas.PROBES):
        if name != "claude-api":
            monkeypatch.setitem(quotas.PROBES, name, lambda: {"status": "ok", "windows": []})

    out = quotas.refresh_all(db / "quotas.json")

    assert out["providers"]["claude-api"]["status"] == "ok"
    assert out["providers"]["claude-api"]["spent_usd"] == 0


# ---- the stop line in dispatch and continue ---------------------------------


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


def test_dispatch_refuses_at_the_stop_line_and_force_overrides(
    db, configured, quiet
) -> None:
    configured({"agents": {"claude-api": {**_API, "stop_at_usd": 10}}})
    _seed("claude-api", 6.0, datetime.now(UTC))
    _seed("claude-api", 4.0, datetime.now(UTC))

    refused = runner.invoke(
        cli.app, ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(quiet), "--sync"]
    )

    assert refused.exit_code == 3, refused.output
    assert "spent $10.00" in refused.output
    assert "stop_at_usd=10" in refused.output
    assert "--force" in refused.output
    assert len(state.list_sessions(limit=100)) == 2  # no row was inserted

    forced = runner.invoke(
        cli.app,
        ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(quiet), "--sync", "--force"],
    )

    assert forced.exit_code == 0, forced.output
    assert len(state.list_sessions(limit=100)) == 3


def test_below_the_stop_line_dispatch_proceeds_even_when_low(db, configured, quiet) -> None:
    # $0.01 left is `low` in quotas, but the lane burns the credit to zero: only
    # the stop line refuses.
    configured({"agents": {"claude-api": _API}})
    _seed("claude-api", 199.99, datetime.now(UTC))

    result = runner.invoke(
        cli.app, ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(quiet), "--sync"]
    )

    assert result.exit_code == 0, result.output


def test_the_stop_line_only_counts_this_cycle(db, configured, quiet) -> None:
    configured({"agents": {"claude-api": {**_API, "stop_at_usd": 10}}})
    _seed("claude-api", 500.0, datetime.now(UTC) - timedelta(days=40))

    result = runner.invoke(
        cli.app, ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(quiet), "--sync"]
    )

    assert result.exit_code == 0, result.output


def test_continue_is_refused_at_the_stop_line_too(db, configured, quiet) -> None:
    configured({"agents": {"claude-api": {**_API, "stop_at_usd": 10}}})
    parent = _seed("claude-api", 10.0, datetime.now(UTC))
    state.update_session(parent, agent_session_id="sess-api", cwd=str(quiet))

    result = runner.invoke(cli.app, ["continue", parent, "--prompt", "more", "--sync"])

    assert result.exit_code == 3, result.output
    assert "--force" in result.output
    assert len(state.list_sessions(limit=100)) == 1


def test_other_lanes_without_a_stop_line_are_untouched(db, configured, quiet) -> None:
    configured({"agents": {"claude-api": {**_API, "stop_at_usd": 10}}})
    _seed("claude-api", 10.0, datetime.now(UTC))

    result = runner.invoke(
        cli.app, ["dispatch", "codex", "--prompt", "p", "--cwd", str(quiet), "--sync"]
    )

    assert result.exit_code == 0, result.output


# ---- the runner itself: real Node, a stand-in SDK ---------------------------

NODE = shutil.which("node")


def _node_is_usable() -> bool:
    version = claude_sdk.node_version(NODE) if NODE else None
    return version is not None and version[:2] >= claude_sdk.MIN_NODE


needs_node = pytest.mark.skipif(not _node_is_usable(), reason="Node 18.14+ is not on PATH")

# Yields what query() yields, shaped by FAKE_API_KEY_SOURCE and FAKE_RESULT, and
# echoes the prompt and the options it was given in the assistant message.
_STUB_SDK = """\
export async function* query({ prompt, options }) {
  yield { type: "system", subtype: "init", session_id: "sess-stub",
          apiKeySource: process.env.FAKE_API_KEY_SOURCE ?? "ANTHROPIC_API_KEY" };
  const echo = Object.fromEntries(Object.entries(options).filter(
    ([, value]) => typeof value !== "function" && !(value instanceof AbortController)));
  const text = JSON.stringify({ prompt, options: echo });
  yield { type: "assistant", session_id: "sess-stub",
          message: { content: [{ type: "text", text }] } };
  if (process.env.FAKE_RESULT === "credit") {
    yield { type: "result", subtype: "success", is_error: true, session_id: "sess-stub",
            result: "Credit balance is too low", total_cost_usd: 0, num_turns: 1, duration_ms: 5 };
    throw new Error("Claude Code returned an error result: Credit balance is too low");
  }
  yield { type: "result", subtype: "success", is_error: false, session_id: "sess-stub",
          result: "OK", total_cost_usd: 0.0081, num_turns: 1, duration_ms: 1200 };
}
"""


def _install_stub_sdk(home: Path) -> Path:
    sdk = _install_fake_sdk(home)
    package = sdk / "node_modules" / SDK_PACKAGE
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": SDK_PACKAGE,
                "version": SDK_VERSION,
                "type": "module",
                "exports": {".": {"default": "./sdk.mjs"}},
            }
        ),
        encoding="utf-8",
    )
    (package / "sdk.mjs").write_text(_STUB_SDK, encoding="utf-8")
    return sdk


def _run_runner(sdk: Path, cwd: Path, *args: str, prompt: str = "say OK", **env: str):
    return subprocess.run(
        [NODE, str(RUNNER_PATH), "--sdk-dir", str(sdk), *args],
        input=prompt,
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _echo(lines: list[dict]) -> dict:
    (assistant,) = [line for line in lines if line["type"] == "assistant"]
    return json.loads(assistant["message"]["content"][0]["text"])


@needs_node
def test_the_runner_streams_every_message_and_maps_the_cli_flags(tmp_path) -> None:
    sdk = _install_stub_sdk(tmp_path / "home")

    proc = _run_runner(
        sdk,
        tmp_path,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read, Edit,Bash(pytest:*)",
        "--disallowedTools", "WebFetch",
        "--model", "haiku",
        "--effort", "high",
        "--max-turns", "7",
        prompt="Reply with the single word OK",
    )

    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [line["type"] for line in lines] == ["system", "assistant", "result"]
    assert lines[0]["session_id"] == "sess-stub"
    assert lines[-1]["total_cost_usd"] == 0.0081
    echo = _echo(lines)
    assert echo["prompt"] == "Reply with the single word OK"
    assert echo["options"] == {
        "cwd": str(tmp_path.resolve()),
        "systemPrompt": {"type": "preset", "preset": "claude_code"},
        "tools": {"type": "preset", "preset": "claude_code"},
        "settingSources": [],
        "strictMcpConfig": True,
        "model": "haiku",
        "effort": "high",
        "maxTurns": 7,
        "permissionMode": "acceptEdits",
        "allowedTools": ["Read", "Edit", "Bash(pytest:*)"],
        "disallowedTools": ["WebFetch"],
    }


@needs_node
def test_the_runner_in_json_mode_prints_the_result_alone_and_resumes(tmp_path) -> None:
    sdk = _install_stub_sdk(tmp_path / "home")

    proc = _run_runner(sdk, tmp_path, "--resume", "sess-old", "--output-format", "json")

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert (result["type"], result["result"], result["session_id"]) == (
        "result",
        "OK",
        "sess-stub",
    )


@needs_node
def test_the_runner_maps_yolo_to_bypass_permissions(tmp_path) -> None:
    sdk = _install_stub_sdk(tmp_path / "home")

    proc = _run_runner(sdk, tmp_path, "--dangerously-skip-permissions", "--resume", "sess-old")

    assert proc.returncode == 0, proc.stderr
    options = _echo([json.loads(line) for line in proc.stdout.splitlines()])["options"]
    assert options["permissionMode"] == "bypassPermissions"
    assert options["allowDangerouslySkipPermissions"] is True
    assert options["resume"] == "sess-old"


@needs_node
def test_the_runner_exits_1_with_the_api_error_the_classifier_reads_as_quota(tmp_path) -> None:
    sdk = _install_stub_sdk(tmp_path / "home")

    proc = _run_runner(sdk, tmp_path, FAKE_RESULT="credit")

    assert proc.returncode == 1
    assert proc.stderr.strip() == "Credit balance is too low"
    assert json.loads(proc.stdout.splitlines()[-1])["is_error"] is True
    assert events.classify_failure(proc.stderr) == "quota"


@needs_node
def test_the_runner_stops_when_the_sdk_did_not_authenticate_with_the_key(tmp_path) -> None:
    # "none" is a claude.ai OAuth login: the subscription the lane must never spend.
    sdk = _install_stub_sdk(tmp_path / "home")

    proc = _run_runner(sdk, tmp_path, FAKE_API_KEY_SOURCE="none")

    assert proc.returncode == 1
    assert 'authenticated with "none", not ANTHROPIC_API_KEY' in proc.stderr
    assert [json.loads(line)["type"] for line in proc.stdout.splitlines()] == ["system"]


@needs_node
@pytest.mark.parametrize(
    ("args", "prompt", "message"),
    [
        ([], "p", "is not installed in"),
        (["--bogus"], "p", "Unknown option '--bogus'"),
        (["--output-format", "text"], "p", "--output-format wants stream-json or json"),
        (["--max-turns", "0"], "p", "--max-turns wants a positive integer"),
        ([], "  ", "no prompt on stdin"),
    ],
)
def test_the_runner_exits_2_on_a_usage_error(tmp_path, args, prompt, message) -> None:
    sdk = tmp_path / "empty" if message == "is not installed in" else _install_stub_sdk(tmp_path)

    proc = _run_runner(sdk, tmp_path, *args, prompt=prompt)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert proc.stdout == ""


@needs_node
def test_the_handler_drives_the_real_runner_end_to_end(
    db, configured, monkeypatch, tmp_path
) -> None:
    configured({"agents": {"claude-api": {"binary": NODE}}})
    _install_stub_sdk(db)
    _with_key(monkeypatch)
    started: list[str] = []

    result = ClaudeSdkHandler().dispatch(
        prompt="say OK", cwd=tmp_path, model="haiku", on_session_started=started.append
    )

    assert started == ["sess-stub"]
    assert (result.agent_session_id, result.initial_output) == ("sess-stub", "OK")
    assert (result.cost_usd, result.duration_seconds) == (0.0081, 1.2)


# ---- playmaker sdk install --------------------------------------------------


def _fake_npm_pair(tmp_path: Path, npm_body: str) -> Path:
    """A fake `node` and, next to it, a fake `npm`; returns the node path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    node = _fake_node(bindir / "node", "sys.exit(0)\n")
    npm = bindir / "npm"
    npm.write_text(f"#!{sys.executable}\n" + textwrap.dedent(npm_body), encoding="utf-8")
    npm.chmod(0o755)
    return node


_GOOD_NPM = """\
import json, os, pathlib, sys
manifest = json.loads(pathlib.Path("package.json").read_text())
((name, version),) = manifest["dependencies"].items()
package = pathlib.Path("node_modules", name)
package.mkdir(parents=True, exist_ok=True)
(package / "package.json").write_text(json.dumps({"name": name, "version": version}))
pathlib.Path("npm-call.json").write_text(
    json.dumps({"argv": sys.argv[1:], "path": os.environ["PATH"]})
)
"""


def test_sdk_install_pins_the_version_with_the_lanes_own_npm(db, configured, tmp_path) -> None:
    node = _fake_npm_pair(tmp_path, _GOOD_NPM)
    configured({"agents": {"claude-api": {"binary": str(node)}}})

    target, version = claude_sdk.install_sdk()

    assert (target, version) == (db / "sdk-runner", SDK_VERSION)
    manifest = json.loads((target / "package.json").read_text())
    assert manifest["dependencies"] == {SDK_PACKAGE: SDK_VERSION}
    call = json.loads((target / "npm-call.json").read_text())
    assert call["argv"] == ["install", "--no-audit", "--no-fund", "--omit=dev"]
    assert call["path"].split(os.pathsep)[0] == str(node.parent)
    assert claude_sdk.installed_sdk_version() == SDK_VERSION


def test_the_sdk_install_command_reports_the_installed_version(db, configured, tmp_path) -> None:
    node = _fake_npm_pair(tmp_path, _GOOD_NPM)
    configured({"agents": {"claude-api": {"binary": str(node)}}})

    result = runner.invoke(cli.app, ["sdk", "install"])

    assert result.exit_code == 0, result.output
    assert f"installed {SDK_PACKAGE}@{SDK_VERSION}" in result.output


def test_a_failed_npm_install_exits_1_with_npms_last_lines(db, configured, tmp_path) -> None:
    node = _fake_npm_pair(
        tmp_path, 'import sys\nsys.stderr.write("npm error code E404\\n")\nsys.exit(1)\n'
    )
    configured({"agents": {"claude-api": {"binary": str(node)}}})

    result = runner.invoke(cli.app, ["sdk", "install"])

    assert result.exit_code == 1
    assert "npm install" in result.output
    assert "failed (exit 1)" in result.output
    assert "E404" in result.output
    assert claude_sdk.installed_sdk_version() is None

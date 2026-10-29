"""The claude-api lane: `claude --bare` on an API key, and its local credit counter."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
import playmaker.quotas as quotas
import playmaker.registry as registry
import playmaker.state as state
from playmaker import cli
from playmaker.agents import claude as claude_mod
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


class _FakePopen:
    """Stand-in for subprocess.Popen that records the command and its keyword arguments."""

    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self._lines = lines
        self.returncode = returncode
        self._stderr = stderr
        self.cmd: list[str] = []
        self.kwargs: dict = {}
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
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


# ---- the lane itself --------------------------------------------------------


def test_registry_has_claude_api_as_a_bare_claude_lane(configured) -> None:
    configured({})
    handler = registry.get_handler("claude-api")

    assert isinstance(handler, ClaudeHandler)
    assert handler.name == "claude-api"
    assert handler.bare
    assert not registry.get_handler("claude").bare
    assert registry.claude_lanes() == ("claude", "claude-api")


def test_an_unconfigured_claude_api_lane_runs_the_claude_binary(configured) -> None:
    # One CLI, two lanes: the lane name is not an executable.
    configured({})

    assert agent_binary("claude-api") == "claude"
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


# ---- command shape and environment ------------------------------------------


def test_dispatch_runs_claude_bare_with_the_key_in_the_environment_only(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _with_key(monkeypatch)
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    result = ClaudeHandler("claude-api", bare=True).dispatch(
        prompt="p", cwd=tmp_path, model="haiku"
    )

    assert fake.cmd[:3] == ["claude", "-p", "--bare"]
    assert fake.cmd[fake.cmd.index("--model") + 1] == "haiku"
    assert fake.kwargs["env"][API_KEY_ENV_VAR] == KEY
    assert KEY not in " ".join(fake.cmd)
    assert API_KEY_ENV_VAR not in os.environ  # the parent process is untouched
    assert result.cost_usd == 0.0123


def test_the_subscription_lane_sends_no_bare_flag_and_inherits_its_environment(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "--bare" not in fake.cmd
    assert fake.kwargs["env"] is None


def test_resume_runs_claude_bare_with_the_key_in_the_environment(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _with_key(monkeypatch)
    recorded: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        recorded.append((cmd, kwargs))
        return _Completed(
            stdout=json.dumps({"session_id": "sess-api", "result": "ok", "duration_ms": 10})
        )

    monkeypatch.setattr("playmaker.agents.claude.subprocess.run", fake_run)

    ClaudeHandler("claude-api", bare=True).resume(
        prompt="more", cwd=tmp_path, agent_session_id="sess-api"
    )

    ((cmd, kwargs),) = recorded
    assert cmd[:5] == ["claude", "-p", "--bare", "--resume", "sess-api"]
    assert kwargs["env"][API_KEY_ENV_VAR] == KEY
    assert KEY not in " ".join(cmd)


def test_a_missing_key_refuses_before_any_process_starts(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    monkeypatch.setattr(claude_mod.sys, "platform", "darwin")
    monkeypatch.setattr(claude_mod, "_keychain_api_key", lambda: None)
    fake = _FakePopen(_success_lines())
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match=re.escape(KEYCHAIN_ADD_COMMAND)):
        ClaudeHandler("claude-api", bare=True).dispatch(prompt="p", cwd=tmp_path)

    assert fake.cmd == []


def test_a_failure_message_names_the_lane_and_carries_no_key(
    configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _with_key(monkeypatch)
    fake = _FakePopen(_success_lines()[:1], returncode=1, stderr="Invalid API key")
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="claude-api failed") as info:
        ClaudeHandler("claude-api", bare=True).dispatch(prompt="p", cwd=tmp_path)

    assert KEY not in str(info.value)


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
    db, configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _claude_on_path(monkeypatch)
    monkeypatch.setattr(claude_mod.sys, "platform", "darwin")
    monkeypatch.setattr(claude_mod, "_keychain_api_key", lambda: None)

    result = runner.invoke(
        cli.app, ["dispatch", "claude-api", "--prompt", "p", "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 2, result.output
    assert "no API key in the macOS Keychain" in result.output
    assert "security add-generic-password -s playmaker-anthropic-api" in result.output
    assert state.list_sessions() == []


def test_a_sync_dispatch_records_the_cost_and_never_prints_the_key(
    db, configured, monkeypatch, tmp_path
) -> None:
    configured({})
    _claude_on_path(monkeypatch)
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
    assert "--bare" in fake.cmd
    assert fake.kwargs["env"][API_KEY_ENV_VAR] == KEY
    (row,) = state.list_sessions()
    assert row["agent"] == "claude-api"
    assert row["cost_usd"] == 0.0123


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

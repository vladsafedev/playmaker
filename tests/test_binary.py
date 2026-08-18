"""Which executable each handler actually launches: [agents.<name>] binary."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
from playmaker.config import agent_binary
from playmaker.registry import all_handlers

AGENTS = ("claude", "codex", "agy", "gemini", "opencode")


@pytest.fixture
def configured(monkeypatch):
    """Install a fake config.toml body for the duration of a test."""

    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


# ---- resolution -------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_an_unconfigured_agent_runs_its_own_name(configured, agent) -> None:
    configured({})

    assert agent_binary(agent) == agent


@pytest.mark.parametrize("agent", AGENTS)
def test_the_configured_binary_wins(configured, agent) -> None:
    configured({"agents": {agent: {"binary": f"/opt/{agent}-nightly/bin/{agent}"}}})

    assert agent_binary(agent) == f"/opt/{agent}-nightly/bin/{agent}"


def test_a_leading_tilde_is_expanded(configured) -> None:
    # The whole point of the setting is pointing at a CLI that installs under
    # $HOME; subprocess does no shell expansion, so a literal "~" would ENOENT.
    configured({"agents": {"opencode": {"binary": "~/.opencode/bin/opencode"}}})

    resolved = agent_binary("opencode")

    assert not resolved.startswith("~")
    assert resolved == str(Path("~/.opencode/bin/opencode").expanduser())


def test_an_empty_binary_falls_back_to_the_agent_name(configured) -> None:
    # `binary = ""` is a config typo, not a request to exec the empty string.
    configured({"agents": {"agy": {"binary": "   "}}})

    assert agent_binary("agy") == "agy"


def test_one_agents_binary_does_not_leak_into_another(configured) -> None:
    configured({"agents": {"claude": {"binary": "/opt/claude-nightly"}}})

    assert agent_binary("claude") == "/opt/claude-nightly"
    assert agent_binary("codex") == "codex"


# ---- availability -----------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_availability_probes_the_configured_binary(monkeypatch, configured, agent) -> None:
    # `playmaker dispatch` refuses to start when this says no, so a handler that
    # ignored the setting would report "not installed" for a working CLI.
    configured({"agents": {agent: {"binary": f"/opt/{agent}/bin/{agent}"}}})
    probed: list[str] = []

    def fake_which(cmd, *args, **kwargs):
        probed.append(cmd)
        return cmd

    for module in ("claude", "codex", "agy", "gemini", "opencode"):
        monkeypatch.setattr(f"playmaker.agents.{module}.shutil.which", fake_which)

    assert all_handlers()[agent].is_available() is True
    assert probed == [f"/opt/{agent}/bin/{agent}"]


@pytest.mark.parametrize("agent", AGENTS)
def test_availability_falls_back_to_the_bare_name(monkeypatch, configured, agent) -> None:
    configured({})
    probed: list[str] = []

    def fake_which(cmd, *args, **kwargs):
        probed.append(cmd)
        return None

    for module in ("claude", "codex", "agy", "gemini", "opencode"):
        monkeypatch.setattr(f"playmaker.agents.{module}.shutil.which", fake_which)

    assert all_handlers()[agent].is_available() is False
    assert probed == [agent]


# ---- the launched command ---------------------------------------------------


def test_claude_dispatch_execs_the_configured_binary(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"claude": {"binary": "/opt/claude-nightly/bin/claude"}}})

    assert _dispatch_argv0("claude", monkeypatch, tmp_path) == "/opt/claude-nightly/bin/claude"


def test_codex_dispatch_execs_the_configured_binary(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"codex": {"binary": "/opt/codex/bin/codex"}}})

    assert _dispatch_argv0("codex", monkeypatch, tmp_path) == "/opt/codex/bin/codex"


def test_gemini_dispatch_execs_the_configured_binary(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"gemini": {"binary": "/opt/gemini/bin/gemini"}}})

    assert _dispatch_argv0("gemini", monkeypatch, tmp_path) == "/opt/gemini/bin/gemini"


def test_agy_dispatch_execs_the_configured_binary(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"agy": {"binary": "/opt/agy/bin/agy"}}})

    assert _dispatch_argv0("agy", monkeypatch, tmp_path) == "/opt/agy/bin/agy"


def test_opencode_dispatch_execs_the_configured_binary(monkeypatch, tmp_path, configured) -> None:
    # The motivating case: opencode installs to ~/.opencode/bin, which only an
    # interactive .zshrc puts on PATH — a detached dispatch needs this setting.
    configured({"agents": {"opencode": {"binary": "~/.opencode/bin/opencode"}}})
    expected = str(Path("~/.opencode/bin/opencode").expanduser())

    assert _dispatch_argv0("opencode", monkeypatch, tmp_path) == expected


def _dispatch_argv0(agent: str, monkeypatch, tmp_path: Path) -> str:
    """argv[0] of the process a dispatch would have spawned.

    Every handler streams its child's stdout, so the fake has to be a Popen
    stand-in rather than a plain recorder; we let each dispatch fail after the
    exec and read the recorded command out of the closure.
    """
    recorded: list[list[str]] = []

    class _FakePopen:
        """Enough of Popen for every handler's read loop: agy polls and writes
        to file handles, the others iterate stdout and read stderr."""

        returncode = 1

        def __init__(self, cmd, **kwargs):
            recorded.append(cmd)
            self.stdout = iter(())
            self.stderr = _Reader()

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    class _Reader:
        @staticmethod
        def read() -> str:
            return "stubbed"

    monkeypatch.setattr(f"playmaker.agents.{agent}.subprocess.Popen", _FakePopen)
    if agent == "opencode":
        # Model validation shells out to `opencode models`; keep it out of the way.
        from playmaker.agents.opencode import OpencodeHandler

        monkeypatch.setattr(OpencodeHandler, "available_models", staticmethod(tuple))
    if agent == "agy":
        from playmaker.agents.agy import AgyHandler

        monkeypatch.setattr(AgyHandler, "available_models", staticmethod(tuple))

    with pytest.raises(RuntimeError):
        all_handlers()[agent].dispatch(prompt="p", cwd=tmp_path)

    assert len(recorded) == 1
    return recorded[0][0]


# ---- the shipped template ---------------------------------------------------


def test_every_agent_in_the_template_declares_the_binary_it_defaults_to() -> None:
    # The template is a contract: a key it lists has to be one a handler reads.
    import tomllib

    from playmaker.cli import _DEFAULT_CONFIG

    cfg = tomllib.loads(_DEFAULT_CONFIG)

    for agent in AGENTS:
        assert cfg["agents"][agent]["binary"] == agent

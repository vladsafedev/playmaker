"""How each handler answers 'what may this agent do unattended?'."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
from playmaker.agents.claude import permission_args
from playmaker.agents.codex import sandbox_args


@pytest.fixture
def configured(monkeypatch):
    """Install a fake config.toml body for the duration of a test."""

    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


def test_claude_defaults_to_accept_edits(configured) -> None:
    configured({})

    assert permission_args() == ["--permission-mode", "acceptEdits"]


def test_claude_permission_mode_is_configurable(configured) -> None:
    configured({"agents": {"claude": {"permission_mode": "plan"}}})

    assert permission_args() == ["--permission-mode", "plan"]


def test_claude_yolo_replaces_the_mode_entirely(configured) -> None:
    configured({"agents": {"claude": {"yolo": True, "permission_mode": "plan"}}})

    assert permission_args() == ["--dangerously-skip-permissions"]


def test_claude_honours_the_legacy_skip_permissions_name(configured) -> None:
    # 0.4 configs in the wild say skip_permissions; they must keep working.
    configured({"agents": {"claude": {"skip_permissions": True}}})

    assert permission_args() == ["--dangerously-skip-permissions"]


def test_claude_skip_permissions_false_no_longer_means_do_nothing(configured) -> None:
    # In 0.4 this produced a run that wrote nothing. It now falls through to
    # the default mode, which can actually finish the work.
    configured({"agents": {"claude": {"skip_permissions": False}}})

    assert permission_args() == ["--permission-mode", "acceptEdits"]


def test_claude_tool_lists_are_comma_joined(configured) -> None:
    # Comma-joined on purpose: claude's --allowedTools is variadic and would
    # otherwise swallow the positional prompt that follows it.
    configured(
        {
            "agents": {
                "claude": {
                    "allowed_tools": ["Read", "Edit", "Bash(pytest:*)"],
                    "disallowed_tools": ["WebFetch"],
                }
            }
        }
    )

    args = permission_args()

    assert args[args.index("--allowedTools") + 1] == "Read,Edit,Bash(pytest:*)"
    assert args[args.index("--disallowedTools") + 1] == "WebFetch"


def test_claude_tool_list_accepts_a_plain_string(configured) -> None:
    configured({"agents": {"claude": {"allowed_tools": "Read, Edit"}}})

    args = permission_args()

    assert args[args.index("--allowedTools") + 1] == "Read,Edit"


def test_codex_passes_no_sandbox_flag_by_default(configured) -> None:
    configured({})

    assert sandbox_args() == []


def test_codex_sandbox_policy_is_forwarded(configured) -> None:
    configured({"agents": {"codex": {"sandbox": "read-only"}}})

    assert sandbox_args() == ["-s", "read-only"]


def test_agy_yolo_defaults_on_because_it_has_no_middle_tier(configured) -> None:
    configured({})

    assert config.yolo_enabled("agy", default=True) is True
    assert config.yolo_enabled("claude") is False


def test_agy_yolo_can_be_turned_off(configured) -> None:
    configured({"agents": {"agy": {"yolo": False}}})

    assert config.yolo_enabled("agy", default=True) is False


def test_opencode_yolo_defaults_on_for_the_same_reason_as_agy(configured) -> None:
    # --auto is opencode's only permission lever; without it a detached run can
    # come back having done nothing.
    configured({})

    assert config.yolo_enabled("opencode", default=True) is True


def test_opencode_yolo_can_be_turned_off(configured) -> None:
    configured({"agents": {"opencode": {"yolo": False}}})

    assert config.yolo_enabled("opencode", default=True) is False


def test_shipped_config_template_is_valid_toml_and_matches_the_defaults() -> None:
    # `playmaker init` writes this verbatim, so a typo here reaches every new
    # user, and a drift from the handler defaults is a documentation lie.
    import tomllib

    from playmaker.agents.claude import DEFAULT_PERMISSION_MODE
    from playmaker.cli import _DEFAULT_CONFIG

    cfg = tomllib.loads(_DEFAULT_CONFIG)

    assert cfg["agents"]["claude"]["permission_mode"] == DEFAULT_PERMISSION_MODE
    assert cfg["agents"]["agy"]["yolo"] is True
    assert cfg["agents"]["opencode"]["yolo"] is True
    assert "yolo" not in cfg["agents"]["claude"]  # the escape hatch stays commented out
    assert "sandbox" not in cfg["agents"]["codex"]
    # No default model: a shipped "zai-coding-plan/..." would hard-fail model
    # validation for every new user who has no Z.ai credential.
    assert "model" not in cfg["agents"]["opencode"]
    assert cfg["notifications"]["editor"]

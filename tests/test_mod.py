from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker import cli
from playmaker.cli import MOD_MARKETPLACE, MOD_NAME, _bundled_mod_dir, app

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def claude_calls(monkeypatch) -> list[list[str]]:
    # `claude plugin …` never runs for real in tests; every call is recorded.
    calls: list[list[str]] = []

    def fake(args: list[str]) -> tuple[bool, str]:
        calls.append(args)
        return True, ""

    monkeypatch.setattr(cli, "_claude_plugin", fake)
    return calls


def test_bundled_mod_is_findable_from_a_source_checkout() -> None:
    manifest = json.loads((_bundled_mod_dir() / ".claude-plugin" / "plugin.json").read_text())

    assert manifest["name"] == MOD_NAME
    assert (_bundled_mod_dir() / "hooks" / "register.tsx").is_file()


def test_the_repository_is_a_marketplace_listing_the_mod() -> None:
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == MOD_NAME)

    assert (REPO / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    # A checkout's marketplace and `mod install`'s must not share a name.
    assert market["name"] != MOD_MARKETPLACE


def test_mod_install_copies_the_mod_and_its_marketplace(
    tmp_path: Path, claude_calls: list[list[str]]
) -> None:
    result = runner.invoke(app, ["mod", "install", "--dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / MOD_NAME / "hooks" / "register.tsx").is_file()
    market = json.loads((tmp_path / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == MOD_MARKETPLACE
    assert market["plugins"] == [{"name": MOD_NAME, "source": f"./{MOD_NAME}"}]
    assert claude_calls == [
        ["marketplace", "add", str(tmp_path)],
        ["install", f"{MOD_NAME}@{MOD_MARKETPLACE}"],
    ]


def test_mod_install_no_register_only_prints_the_steps(
    tmp_path: Path, claude_calls: list[list[str]]
) -> None:
    result = runner.invoke(app, ["mod", "install", "--dir", str(tmp_path), "--no-register"])

    assert result.exit_code == 0
    assert claude_calls == []
    assert f"claude plugin install {MOD_NAME}@{MOD_MARKETPLACE}" in result.output


def test_mod_install_refuses_to_clobber_without_force(
    tmp_path: Path, claude_calls: list[list[str]]
) -> None:
    runner.invoke(app, ["mod", "install", "--dir", str(tmp_path)])

    again = runner.invoke(app, ["mod", "install", "--dir", str(tmp_path)])
    forced = runner.invoke(app, ["mod", "install", "--dir", str(tmp_path), "--force"])

    assert again.exit_code == 1
    assert forced.exit_code == 0


def test_mod_install_reports_a_failed_registration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_claude_plugin", lambda args: (False, "claude: not found"))

    result = runner.invoke(app, ["mod", "install", "--dir", str(tmp_path)])

    assert result.exit_code == 1
    # The copy stands; the steps left to run are printed.
    assert (tmp_path / MOD_NAME / "hooks" / "register.tsx").is_file()
    assert f"claude plugin install {MOD_NAME}@{MOD_MARKETPLACE}" in result.output

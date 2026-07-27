from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker.cli import SKILL_NAME, _bundled_skill_dir, app

runner = CliRunner()


def test_bundled_skill_is_findable_from_a_source_checkout() -> None:
    skill = _bundled_skill_dir() / "SKILL.md"

    assert skill.is_file()
    assert skill.read_text(encoding="utf-8").lstrip().startswith("---")


def test_skill_install_writes_the_skill(tmp_path: Path) -> None:
    result = runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / SKILL_NAME / "SKILL.md").is_file()


def test_skill_install_refuses_to_clobber_without_force(tmp_path: Path) -> None:
    runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)])
    target = tmp_path / SKILL_NAME / "SKILL.md"
    target.write_text("edited by hand", encoding="utf-8")

    result = runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)])

    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == "edited by hand"


def test_skill_install_force_overwrites(tmp_path: Path) -> None:
    runner.invoke(app, ["skill", "install", "--dir", str(tmp_path)])
    target = tmp_path / SKILL_NAME / "SKILL.md"
    target.write_text("edited by hand", encoding="utf-8")

    result = runner.invoke(app, ["skill", "install", "--dir", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") != "edited by hand"

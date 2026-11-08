from __future__ import annotations

from pathlib import Path

import pytest

from playmaker.agents.codex import CODEX_EFFORT_ENV_VAR


@pytest.fixture(autouse=True)
def isolate_codex_environment(monkeypatch, tmp_path: Path) -> None:
    """Keep codex-lane tests independent of the developer's live settings."""
    monkeypatch.delenv(CODEX_EFFORT_ENV_VAR, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

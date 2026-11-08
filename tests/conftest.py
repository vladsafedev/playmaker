from __future__ import annotations

from pathlib import Path

import pytest

import playmaker.config as config
from playmaker.agents.codex import CODEX_EFFORT_ENV_VAR


@pytest.fixture(autouse=True)
def isolate_codex_environment(monkeypatch, tmp_path: Path):
    """Keep tests independent of the developer's live settings.

    CODEX_HOME and PLAYMAKER_CODEX_EFFORT isolate the codex lane; the config
    patch points playmaker at an empty temp config.toml, so a real
    `[agents.codex] reasoning_effort` (or any other live setting) cannot
    change what a test sees. Tests that need a config keep monkeypatching
    `load_config` with their own body.
    """
    monkeypatch.delenv(CODEX_EFFORT_ENV_VAR, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    config_path = tmp_path / "playmaker-config.toml"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    # Hold the real loader: a test may replace config.load_config with its own
    # body, and this fixture's teardown runs before monkeypatch restores it.
    load = config.load_config
    load.cache_clear()
    yield
    load.cache_clear()

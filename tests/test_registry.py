from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.registry as registry


@pytest.mark.parametrize("name", ["claude", "codex", "agy", "gemini"])
def test_get_handler_returns_named_handler(name: str) -> None:
    handler = registry.get_handler(name)

    assert handler.name == name


def test_all_handlers_contains_builtin_agents() -> None:
    handlers = registry.all_handlers()

    assert {"claude", "codex", "agy", "gemini"} <= set(handlers)


def test_get_handler_unknown_agent_raises() -> None:
    with pytest.raises(KeyError):
        registry.get_handler("bogus")

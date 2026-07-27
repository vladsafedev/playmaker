"""Read-only access to ~/.playmaker/config.toml."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from typing import Any

from playmaker.state import CONFIG_PATH


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Parse config.toml once per process; missing or broken file -> {}."""
    try:
        with CONFIG_PATH.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def agent_setting(agent: str, key: str, default: Any = None) -> Any:
    """Look up [agents.<agent>] <key>, falling back to `default`."""
    return load_config().get("agents", {}).get(agent, {}).get(key, default)


def setting(section: str, key: str, default: Any = None) -> Any:
    """Look up [<section>] <key>, falling back to `default`."""
    value = load_config().get(section, {})
    return value.get(key, default) if isinstance(value, dict) else default

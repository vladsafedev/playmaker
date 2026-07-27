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


def yolo_enabled(agent: str, *, default: bool = False) -> bool:
    """Whether this agent is configured to skip its permission checks entirely.

    `yolo` is the current spelling; `skip_permissions` is the 0.4 name and is
    still honoured so existing configs keep working.
    """
    value = agent_setting(agent, "yolo")
    if value is None:
        value = agent_setting(agent, "skip_permissions")
    return default if value is None else bool(value)


def agent_list_setting(agent: str, key: str) -> list[str]:
    """A list-valued agent setting, tolerating a plain string in the TOML."""
    value = agent_setting(agent, key)
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]

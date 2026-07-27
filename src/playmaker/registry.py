"""Map agent name -> handler instance + profile lookup."""

from __future__ import annotations

from pathlib import Path

from playmaker.agents.agy import AgyHandler
from playmaker.agents.base import AgentHandler
from playmaker.agents.claude import ClaudeHandler
from playmaker.agents.codex import CodexHandler
from playmaker.agents.gemini import GeminiHandler
from playmaker.state import AGENTS_DIR

_HANDLERS: dict[str, AgentHandler] = {
    "claude": ClaudeHandler(),
    "codex": CodexHandler(),
    "agy": AgyHandler(),
    "gemini": GeminiHandler(),
}


def get_handler(name: str) -> AgentHandler:
    if name not in _HANDLERS:
        raise KeyError(f"unknown agent {name!r}; available: {', '.join(_HANDLERS)}")
    return _HANDLERS[name]


def all_handlers() -> dict[str, AgentHandler]:
    return dict(_HANDLERS)


def find_profile(agent_name: str, project_cwd: Path | None = None) -> Path | None:
    """Project-local profile takes precedence over global."""
    candidates: list[Path] = []
    if project_cwd is not None:
        candidates.append(project_cwd / ".playmaker" / "agents" / f"{agent_name}.md")
    candidates.append(AGENTS_DIR / f"{agent_name}.md")
    for path in candidates:
        if path.exists():
            return path
    return None

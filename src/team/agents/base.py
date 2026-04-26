"""Common interfaces and types for agent handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class DispatchResult:
    """Outcome of a synchronous one-shot dispatch."""

    agent_session_id: str
    cwd: str
    session_file: Path | None
    initial_output: str
    cost_usd: float | None = None
    duration_seconds: float | None = None
    exit_code: int = 0


@dataclass
class Turn:
    """One normalized turn extracted from an agent's session file."""

    role: str  # user | assistant | tool | system
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    timestamp: datetime | None = None


class AgentHandler(Protocol):
    """Each supported agent (claude/codex/gemini) implements this Protocol."""

    name: str

    def is_available(self) -> bool:
        """Binary present and reachable on PATH."""
        ...

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
    ) -> DispatchResult:
        """Run agent non-interactively, await first turn, return metadata."""
        ...

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        """Locate the agent's native session file for a given id+cwd."""
        ...

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Parse the session file into a normalized list of turns."""
        ...

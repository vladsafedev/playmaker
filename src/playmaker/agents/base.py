"""Common interfaces and types for agent handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol


# Called by a handler the moment it learns the agent's session id, before the
# dispatch finishes. Codex emits it from a `thread.started` stdout event ~1s in;
# Claude/Gemini have no early signal in non-interactive mode and call it at the
# end. Failures inside the callback must not abort dispatch.
SessionStartedCallback = Callable[[str], None]


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
    """Each supported agent (claude/codex/agy/gemini) implements this Protocol."""

    name: str

    def is_available(self) -> bool:
        """Binary present and reachable on PATH."""
        ...

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        """Run agent non-interactively, await first turn, return metadata.

        Implementations MUST call `on_session_started(agent_session_id)` exactly
        once as soon as the id is known — early if the agent's protocol exposes
        it mid-stream, otherwise just before returning.

        `model` is forwarded to the agent CLI's own `--model` flag when set;
        when None the agent picks its configured default.
        """
        ...

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        """Continue a previously-started agent session with a new prompt.

        The returned `DispatchResult.agent_session_id` may equal the input id
        (most agents keep the same thread on resume) or may differ if the
        agent's protocol mints a new continuation id — callers should rely on
        whatever the result reports.

        Like `dispatch`, MUST call `on_session_started` exactly once.
        """
        ...

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        """Locate the agent's native session file for a given id+cwd."""
        ...

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Parse the session file into a normalized list of turns."""
        ...

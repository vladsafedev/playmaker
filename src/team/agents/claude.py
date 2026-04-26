"""Claude Code CLI handler.

Empirically:
- oneshot: `claude -p "..."`
- json output: `--output-format json`
- primary cwd: subprocess cwd; `--add-dir` is variadic (multiple dirs) and would
  swallow the positional prompt, so we omit it and rely on subprocess cwd alone
- session file: ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-id>.jsonl
- json result fields: session_id, result, total_cost_usd, duration_ms, usage
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from team.agents.base import DispatchResult, Turn


class ClaudeHandler:
    name = "claude"

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
    ) -> DispatchResult:
        full_prompt = self._build_prompt(prompt, files or [])
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            full_prompt,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude returned non-JSON output: {proc.stdout[:500]}") from e

        agent_session_id = data["session_id"]
        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=self.find_session_file(agent_session_id, cwd),
            initial_output=data.get("result", ""),
            cost_usd=data.get("total_cost_usd"),
            duration_seconds=data.get("duration_ms", 0) / 1000.0,
            exit_code=proc.returncode,
        )

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        cwd_hash = self._hash_cwd(cwd)
        path = Path("~/.claude/projects").expanduser() / cwd_hash / f"{agent_session_id}.jsonl"
        return path if path.exists() else None

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Stub for Phase 1; real parser lands in Phase 3."""
        return []

    @staticmethod
    def _hash_cwd(cwd: Path) -> str:
        # Empirical: literal `/` -> `-`, applied to the absolute resolved path.
        # e.g. /Users/x/Sites/foo  ->  -Users-x-Sites-foo
        absolute = str(cwd.expanduser().resolve())
        return absolute.replace("/", "-")

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = " ".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

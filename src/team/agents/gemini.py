"""Gemini CLI handler.

Empirically:
- oneshot: `gemini -p "<prompt>" -o json [--yolo] [--include-directories <list>]`
- session file: ~/.gemini/tmp/<cwd-basename>/chats/session-<timestamp>-<short-id>.<ext>
  where short-id is first 8 chars of session_id
- TWO file formats coexist in chats/:
  - .jsonl  (interactive sessions: line-per-event, types metadata|user|gemini)
  - .json   (single object {sessionId, projectHash, startTime, kind, messages: [...]})
    written by non-interactive `-p` runs
- stdout json output: {session_id, response, stats: {models: {...}}}
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from team.agents.base import DispatchResult, Turn


GEMINI_CHATS_ROOT = Path("~/.gemini/tmp").expanduser()


class GeminiHandler:
    name = "gemini"

    def is_available(self) -> bool:
        return shutil.which("gemini") is not None

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
    ) -> DispatchResult:
        full_prompt = self._build_prompt(prompt, files or [])
        cmd = [
            "gemini",
            "-p",
            full_prompt,
            "-o",
            "json",
            "--yolo",
        ]
        t0 = time.monotonic()
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        duration = time.monotonic() - t0

        if proc.returncode != 0:
            raise RuntimeError(
                f"gemini failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"gemini returned non-JSON output: {proc.stdout[:500]}") from e

        agent_session_id = data.get("session_id") or data.get("sessionId") or ""
        if not agent_session_id:
            raise RuntimeError(f"gemini json missing session_id: {list(data.keys())}")

        return DispatchResult(
            agent_session_id=agent_session_id,
            cwd=str(cwd),
            session_file=self.find_session_file(agent_session_id, cwd),
            initial_output=data.get("response", ""),
            cost_usd=None,  # Gemini json reports tokens, not USD
            duration_seconds=duration,
            exit_code=proc.returncode,
        )

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        chats_dir = GEMINI_CHATS_ROOT / cwd.name / "chats"
        if not chats_dir.is_dir():
            return None
        short_id = agent_session_id[:8]
        matches: list[Path] = []
        for ext in ("json", "jsonl"):
            matches.extend(chats_dir.glob(f"session-*-{short_id}.{ext}"))
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return None
        # Prefer the one whose first JSON value carries this exact sessionId.
        for path in matches:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    if path.suffix == ".jsonl":
                        head = fh.readline().strip()
                        if not head:
                            continue
                        obj = json.loads(head)
                    else:
                        obj = json.load(fh)
                if obj.get("sessionId") == agent_session_id:
                    return path
            except (OSError, json.JSONDecodeError):
                continue
        return matches[0]

    def parse_session_file(self, path: Path) -> list[Turn]:
        """Stub for Phase 2; full parser in Phase 3."""
        return []

    @staticmethod
    def _build_prompt(prompt: str, files: list[Path]) -> str:
        if not files:
            return prompt
        refs = "\n".join(f"@{p}" for p in files)
        return f"{prompt}\n\n{refs}"

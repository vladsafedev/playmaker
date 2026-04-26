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
        """Read Claude jsonl and yield user/assistant turns.

        Each line is a top-level dict with `type` and `timestamp`. Relevant
        types are `user` and `assistant`; `message.content` is either a
        plain string (simple user input) or an array of typed blocks
        (`text`, `thinking`, `tool_use`, `tool_result`).
        """
        from datetime import datetime

        turns: list[Turn] = []
        if not path.exists():
            return turns
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind not in ("user", "assistant"):
                    continue
                msg = obj.get("message") or {}
                role = msg.get("role") or kind
                content_raw = msg.get("content")

                texts: list[str] = []
                tool_calls: list[dict] = []
                tool_results: list[dict] = []
                if isinstance(content_raw, str):
                    texts.append(content_raw)
                elif isinstance(content_raw, list):
                    for block in content_raw:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            texts.append(block.get("text", ""))
                        elif btype == "thinking":
                            # surface thinking for debugging when --include-tools
                            # is on; coach skill controls when to ask for it
                            texts.append(f"[thinking] {block.get('thinking', '')}")
                        elif btype == "tool_use":
                            tool_calls.append(
                                {
                                    "id": block.get("id"),
                                    "name": block.get("name"),
                                    "input": block.get("input"),
                                }
                            )
                        elif btype == "tool_result":
                            content = block.get("content")
                            tool_results.append(
                                {
                                    "tool_use_id": block.get("tool_use_id"),
                                    "content": content
                                    if isinstance(content, str)
                                    else json.dumps(content) if content else "",
                                }
                            )

                ts_raw = obj.get("timestamp")
                ts = None
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        pass

                turns.append(
                    Turn(
                        role=role,
                        content="\n".join(t for t in texts if t),
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        timestamp=ts,
                    )
                )
        return turns

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

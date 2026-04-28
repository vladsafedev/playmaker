"""ChildHandle — async stdio wrapper around an ACP child subprocess.

(§5) JSON-RPC framing here is newline-delimited: one message per line.
We do NOT use Content-Length framing — Zed's ACP transport over stdio
is line-delimited per agent-client-protocol convention (confirmed by
all reference traces in ~/acp-logs/).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("playmaker.acp.child")


class ChildSpawnError(RuntimeError):
    """Failed to spawn the child subprocess (binary missing, exec error)."""


@dataclass
class ChildHandle:
    """A live child subprocess with line-buffered JSON-RPC stdio.

    Owned by exactly one ChildSession. On shutdown: SIGTERM, wait 3s,
    SIGKILL (§7).
    """

    proc: asyncio.subprocess.Process
    stderr_tail: deque[str]
    _next_id: int = 0

    def alloc_request_id(self) -> int:
        """Allocate a fresh jsonrpc id for a request we send to the child.

        Per-child counter. Zed's id space is independent.
        """
        self._next_id += 1
        return self._next_id

    def is_alive(self) -> bool:
        return self.proc.returncode is None

    async def send(self, msg: dict[str, Any]) -> None:
        """Write one JSON-RPC frame (newline-delimited) to child stdin."""
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            raise ChildSpawnError("child stdin closed")
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def recv(self) -> dict[str, Any] | None:
        """Read one JSON-RPC frame from child stdout.

        Returns None on EOF (child exited or closed stdout).
        Raises json.JSONDecodeError on a malformed line — caller decides
        whether to log+drop or propagate.
        """
        if self.proc.stdout is None:
            return None
        line = await self.proc.stdout.readline()
        if not line:
            return None
        return json.loads(line)


async def spawn_child(cmd: list[str]) -> ChildHandle:
    """Spawn an ACP child with line-buffered stdio.

    Pipes stdin (we write), stdout (we read), stderr (drained into a
    rolling deque for diagnostics — see corr-16).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise ChildSpawnError(f"failed to exec {cmd[0]!r}: {exc}") from exc

    stderr_tail: deque[str] = deque(maxlen=200)

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            stderr_tail.append(text)
            # Mirror to our own stderr so `playmaker acp` operators can
            # see child diagnostics in their wrapper logs.
            print(text, file=sys.stderr, flush=True)

    asyncio.create_task(drain_stderr(), name=f"child-stderr-{proc.pid}")
    return ChildHandle(proc=proc, stderr_tail=stderr_tail)


async def shutdown_child(handle: ChildHandle, timeout: float = 3.0) -> None:
    """SIGTERM + 3s grace + SIGKILL (§7, corr from review)."""
    if handle.proc.returncode is not None:
        return
    try:
        handle.proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(handle.proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            handle.proc.kill()
        except ProcessLookupError:
            pass
        await handle.proc.wait()


def stderr_tail_text(handle: ChildHandle, *, max_lines: int = 20) -> str:
    """Last few stderr lines for embedding in error responses (corr-16)."""
    lines = list(handle.stderr_tail)[-max_lines:]
    return "\n".join(lines).strip() or "(no stderr)"

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker.agents.claude import ClaudeHandler


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> str:
        return "".join(self._lines)


class _FakePopen:
    """Stand-in for subprocess.Popen over claude's stream-json output."""

    def __init__(self, lines: list[str], returncode: int = 0, stderr: str = "") -> None:
        self._lines = lines
        self.returncode = returncode
        self._stderr = stderr
        self.cmd: list[str] = []
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.stdout = _FakeStream(self._lines)
        self.stderr = _FakeStream([self._stderr])
        return self

    def wait(self) -> int:
        return self.returncode


def _events(*objs: dict) -> list[str]:
    return [json.dumps(o) + "\n" for o in objs]


INIT = {"type": "system", "subtype": "init", "session_id": "sess-123"}


def test_dispatch_returns_the_result_event_text(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(
        _events(
            INIT,
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "subtype": "success", "result": "DONE", "total_cost_usd": 0.12},
        )
    )
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    result = ClaudeHandler().dispatch(prompt="do it", cwd=tmp_path)

    assert result.agent_session_id == "sess-123"
    assert result.initial_output == "DONE"
    assert result.cost_usd == 0.12


def test_dispatch_fires_the_session_callback_before_the_run_ends(monkeypatch, tmp_path) -> None:
    # The early callback is what lets `playmaker get` find a detached session
    # within a second instead of after the agent finishes.
    fake = _FakePopen(_events(INIT, {"type": "result", "subtype": "success", "result": "ok"}))
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)
    seen: list[str] = []

    ClaudeHandler().dispatch(prompt="p", cwd=tmp_path, on_session_started=seen.append)

    assert seen == ["sess-123"]


def test_dispatch_surfaces_the_error_from_the_result_event(monkeypatch, tmp_path) -> None:
    # claude -p reports overload/rate-limit/refusal in the final result event
    # with an EMPTY stderr, so without this the failure was a blank message.
    fake = _FakePopen(
        _events(
            INIT,
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "Claude AI usage limit reached"},
        ),
        returncode=1,
    )
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="usage limit reached"):
        ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)


def test_dispatch_falls_back_to_raw_stdout_rather_than_failing_blank(
    monkeypatch, tmp_path
) -> None:
    # No stderr, no result event: the raw first lines are the only diagnostic
    # left, and they beat the blank "claude failed (exit 1):" this replaced.
    fake = _FakePopen(_events(INIT), returncode=1)
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="sess-123"):
        ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)


def test_dispatch_prefers_stderr_over_the_stream_events(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(_events(INIT), returncode=1, stderr="command not found: claude")
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="command not found"):
        ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)


def test_dispatch_without_a_session_id_is_an_error(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(_events({"type": "result", "subtype": "success", "result": "ok"}))
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="missing session_id"):
        ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)


def test_dispatch_skips_permissions_by_default(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(_events(INIT, {"type": "result", "subtype": "success", "result": "ok"}))
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)
    monkeypatch.setattr("playmaker.agents.claude.agent_setting", lambda *a, **k: True)

    ClaudeHandler().dispatch(prompt="p", cwd=tmp_path, model="sonnet")

    assert "--dangerously-skip-permissions" in fake.cmd
    assert fake.cmd[fake.cmd.index("--model") + 1] == "sonnet"


def test_dispatch_honours_skip_permissions_false(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(_events(INIT, {"type": "result", "subtype": "success", "result": "ok"}))
    monkeypatch.setattr("playmaker.agents.claude.subprocess.Popen", fake)
    monkeypatch.setattr("playmaker.agents.claude.agent_setting", lambda *a, **k: False)

    ClaudeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "--dangerously-skip-permissions" not in fake.cmd

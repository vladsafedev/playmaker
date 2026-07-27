from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker.agents.codex import CodexHandler


def test_extract_error_unwraps_nested_json() -> None:
    # codex nests the real message as a JSON string inside error.message
    obj = {
        "type": "turn.failed",
        "error": {
            "message": '{"type":"error","status":400,"error":'
            '{"type":"invalid_request_error","message":'
            "\"The 'gpt-5.6-spark' model is not supported when using Codex\"}}"
        },
    }

    msg = CodexHandler._extract_error(obj)

    assert msg == "The 'gpt-5.6-spark' model is not supported when using Codex"


def test_extract_error_plain_message() -> None:
    obj = {"type": "error", "message": "boom"}

    assert CodexHandler._extract_error(obj) == "boom"


def test_extract_error_error_string() -> None:
    obj = {"type": "turn.failed", "error": "rate limited"}

    assert CodexHandler._extract_error(obj) == "rate limited"


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


class _FakeProc:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = _FakeStream(lines)


def test_consume_stream_captures_turn_failed() -> None:
    lines = [
        '{"type":"thread.started","thread_id":"abc-123"}\n',
        '{"type":"turn.failed","error":{"message":"model X not supported"}}\n',
    ]
    started: list[str] = []

    thread_id, last_message, error_text, _ = CodexHandler._consume_stream(
        _FakeProc(lines), started.append
    )

    assert thread_id == "abc-123"
    assert started == ["abc-123"]
    assert last_message == ""
    assert error_text == "model X not supported"


def test_consume_stream_success_has_no_error() -> None:
    lines = [
        '{"type":"thread.started","thread_id":"t1"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"DONE"}}\n',
    ]

    thread_id, last_message, error_text, _ = CodexHandler._consume_stream(
        _FakeProc(lines), None
    )

    assert thread_id == "t1"
    assert last_message == "DONE"
    assert error_text == ""


class _FakePopen:
    """Stand-in for subprocess.Popen that writes codex's --output-last-message file."""

    def __init__(self, lines: list[str], returncode: int, last_message: str) -> None:
        self._lines = lines
        self.returncode = returncode
        self._last_message = last_message
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_text(self._last_message, encoding="utf-8")
        self.stdout = _FakeStream(self._lines)
        self.stderr = _FakeStream([])
        self.stderr.read = lambda: ""  # type: ignore[method-assign]
        return self

    def wait(self) -> int:
        return self.returncode


_OK_STREAM = [
    '{"type":"thread.started","thread_id":"t1"}\n',
    '{"type":"turn.completed"}\n',
]


def test_dispatch_keeps_answer_written_despite_nonzero_exit(monkeypatch, tmp_path) -> None:
    # codex can exit non-zero on a shutdown hiccup ("failed to record rollout
    # items") after the answer is already on disk — that is not a failure.
    fake = _FakePopen(_OK_STREAM, returncode=1, last_message="ANSWER")
    monkeypatch.setattr("playmaker.agents.codex.subprocess.Popen", fake)

    result = CodexHandler().dispatch(prompt="hi", cwd=tmp_path)

    assert result.initial_output == "ANSWER"
    assert result.agent_session_id == "t1"


def test_dispatch_nonzero_exit_without_answer_raises(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(_OK_STREAM, returncode=1, last_message="")
    monkeypatch.setattr("playmaker.agents.codex.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="codex failed"):
        CodexHandler().dispatch(prompt="hi", cwd=tmp_path)


def test_dispatch_turn_failed_without_answer_raises(monkeypatch, tmp_path) -> None:
    # exit 0 + empty answer + a turn.failed event: the silent-failure case
    lines = [
        '{"type":"thread.started","thread_id":"t1"}\n',
        '{"type":"turn.failed","error":{"message":"model X not supported"}}\n',
    ]
    fake = _FakePopen(lines, returncode=0, last_message="")
    monkeypatch.setattr("playmaker.agents.codex.subprocess.Popen", fake)

    with pytest.raises(RuntimeError, match="model X not supported"):
        CodexHandler().dispatch(prompt="hi", cwd=tmp_path)

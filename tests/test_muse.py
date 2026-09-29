"""Muse handler: JSONL dispatch and native-session parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
from playmaker.agents.muse import (
    MuseHandler,
    find_session_file,
    parse_session_file,
    permission_args,
)

SESSION = "01a0eefe-6295-7b70-8f74-9465c5210290"
FIXTURES = Path(__file__).parent / "fixtures"


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def read(self) -> str:
        return "".join(self.lines)


class _FakePopen:
    def __init__(self, lines: list[str], *, stderr: str = "", returncode: int = 0) -> None:
        self.lines, self.error, self.returncode = lines, stderr, returncode
        self.cmd: list[str] = []
        self.kwargs: dict = {}

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.stdout, self.stderr = _FakeStream(self.lines), _FakeStream([self.error])
        return self

    def wait(self) -> int:
        return self.returncode


@pytest.fixture
def configured(monkeypatch):
    def _set(value: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: value)

    return _set


def _install(monkeypatch, fake: _FakePopen) -> None:
    monkeypatch.setattr("playmaker.agents.muse.subprocess.Popen", fake)


def _fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True)


def test_dispatch_command_callback_and_terminal_output(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"muse": {"model": "configured", "reasoning_effort": "high"}}})
    fake = _FakePopen(_fixture("muse_exec.jsonl"))
    _install(monkeypatch, fake)
    seen: list[str] = []

    result = MuseHandler().dispatch("-do it", tmp_path, [Path("README.txt")], seen.append, "chosen")

    assert fake.cmd == [
        "muse",
        "exec",
        "--json",
        "--model",
        "chosen",
        "--reasoning-effort",
        "high",
        "--disable-approval",
        "--trust-workspace",
        "--",
        "-do it\n\nREADME.txt",
    ]
    assert fake.kwargs["cwd"] == str(tmp_path)
    assert seen == [SESSION]
    assert result.initial_output == "alpha"


def test_resume_command_and_callback_exceptions_are_ignored(
    monkeypatch, tmp_path, configured
) -> None:
    configured({})
    fake = _FakePopen(_fixture("muse_exec.jsonl"))
    _install(monkeypatch, fake)

    result = MuseHandler().resume(
        "again", tmp_path, SESSION, on_session_started=lambda _: (_ for _ in ()).throw(ValueError())
    )

    assert fake.cmd[:5] == ["muse", "exec", "--json", "--session-id", SESSION]
    assert result.agent_session_id == SESSION


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, ["--disable-approval", "--trust-workspace"]),
        ({"yolo": True}, ["--yolo"]),
        ({"trust_workspace": False}, ["--disable-approval"]),
        (
            {"sandbox_network": "restricted", "permission_profile": "safe"},
            [
                "--disable-approval",
                "--trust-workspace",
                "--sandbox-network",
                "restricted",
                "--permission-profile",
                "safe",
            ],
        ),
    ],
)
def test_permission_args(configured, settings, expected) -> None:
    configured({"agents": {"muse": settings}})
    assert permission_args() == expected


def test_failed_terminal_surfaces_reason_and_strips_informational_stderr(
    monkeypatch, tmp_path
) -> None:
    failed = {
        "stream": {"kind": "session", "id": SESSION},
        "payload_type": "run.terminal.failed",
        "payload": {"terminal": "failed", "text": "", "reason": "bad model"},
    }
    fake = _FakePopen(
        [json.dumps(failed) + "\n"],
        stderr="muse: workspace root: x\nmuse: Agent delegation: unavailable\nactual stderr",
        returncode=1,
    )
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="bad model"):
        MuseHandler().dispatch("p", tmp_path)
    assert MuseHandler._error_stderr(fake.error) == "actual stderr"


def test_find_session_file_matches_workspace_and_ignores_subagents(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "data" / "muse" / "sessions" / "2026" / "09" / "30" / SESSION
    root.mkdir(parents=True)
    path = root / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "payload_type": "runtime.session.metadata",
                "payload": {"record": {"workspace_root": str(tmp_path)}},
            }
        )
        + "\n"
    )
    subagent = root / "subagent" / "child" / "session.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_text("{}\n")
    assert find_session_file(SESSION, tmp_path) == path
    assert find_session_file(SESSION, tmp_path / "other") is None


def test_parse_session_file() -> None:
    turns = parse_session_file(FIXTURES / "muse_session.jsonl")

    assert [turn.role for turn in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[0].content.startswith("Read README.txt")
    assert [call["name"] for call in turns[1].tool_calls] == ["read_file", "write_file"]
    assert turns[1].tool_calls[0]["input"] == {"path": "README.txt"}
    assert len(turns[1].tool_results) == 2
    assert turns[1].content == "alpha"
    assert turns[3].content == "hello.txt"

"""Kimi handler: prompt-mode stream dispatch and wire transcript parsing."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
from playmaker.agents.kimi import KimiHandler
from playmaker.registry import all_handlers, get_handler

SESSION = "session_35f81bbc-3038-4bd3-96f1-ababeb5a86cb"
NEWER_SESSION = "session_4c38d6a2-0579-4ff3-8d07-2cc468d6a0f5"
FIXTURE = Path(__file__).parent / "fixtures" / "kimi_wire.jsonl"


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> str:
        return "".join(self._lines)


class _FakePopen:
    """Stand-in for `kimi -p ... --output-format stream-json`."""

    def __init__(self, lines: list[str], *, stderr: str = "", returncode: int = 0) -> None:
        self._lines = lines
        self._stderr = stderr
        self.returncode = returncode
        self.cmd: list[str] = []
        self.kwargs: dict = {}
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout = _FakeStream(self._lines)
        self.stderr = _FakeStream([self._stderr])
        return self

    def wait(self) -> int:
        return self.returncode


class _ConcurrentStderrPopen:
    """Requires stderr draining before its stdout stream can make progress."""

    def __init__(self, lines: list[str], stderr: str) -> None:
        self._lines = lines
        self._stderr = stderr
        self.returncode = 1
        self.stderr_started = threading.Event()
        self.stdout = None
        self.stderr = None

    def __call__(self, cmd, **kwargs):
        class _Stderr(_FakeStream):
            def read(stream_self) -> str:
                self.stderr_started.set()
                return super().read()

        class _Stdout(_FakeStream):
            def __iter__(stream_self):
                assert self.stderr_started.wait(timeout=1), "stderr was not drained concurrently"
                return super().__iter__()

        self.stdout = _Stdout(self._lines)
        self.stderr = _Stderr([self._stderr])
        return self

    def wait(self) -> int:
        return self.returncode


def _events(*objects: dict) -> list[str]:
    return [json.dumps(obj) + "\n" for obj in objects]


def _assistant(content: str | None = None, tool_calls: list[dict] | None = None) -> dict:
    event: dict = {"role": "assistant"}
    if content is not None:
        event["content"] = content
    if tool_calls is not None:
        event["tool_calls"] = tool_calls
    return event


def _resume_hint() -> dict:
    return {"role": "meta", "type": "session.resume_hint", "session_id": SESSION}


@pytest.fixture
def configured(monkeypatch):
    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


@pytest.fixture(autouse=True)
def isolated_kimi_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi-code"))


def _install(monkeypatch, fake: _FakePopen) -> None:
    monkeypatch.setattr("playmaker.agents.kimi.subprocess.Popen", fake)


def test_dispatch_parses_stream_and_announces_the_late_session_id(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(
        _events(
            {"role": "meta", "type": "system.version", "version": "0.41.0"},
            _assistant("Writing hello.txt."),
            _assistant(
                tool_calls=[
                    {
                        "type": "function",
                        "id": "tool_1",
                        "function": {"name": "Write", "arguments": '{"path":"hello.txt"}'},
                    }
                ]
            ),
            {"role": "tool", "tool_call_id": "tool_1", "content": "Wrote 2 bytes"},
            _assistant("DONE"),
            _resume_hint(),
        )
    )
    _install(monkeypatch, fake)
    seen: list[str] = []

    result = KimiHandler().dispatch(prompt="do it", cwd=tmp_path, on_session_started=seen.append)

    assert result.agent_session_id == SESSION
    assert result.initial_output == "DONE"
    assert seen == [SESSION]
    assert fake.kwargs["cwd"] == str(tmp_path)


def test_dispatch_uses_the_last_nonempty_assistant_content(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(
        _events(_assistant("first"), _assistant("   "), _assistant("DONE"), _resume_hint())
    )
    _install(monkeypatch, fake)

    result = KimiHandler().dispatch(prompt="p", cwd=tmp_path)

    assert result.initial_output == "DONE"


def test_missing_resume_hint_uses_the_per_cwd_session_listing(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _FakePopen(_events(_assistant("DONE"))))
    monkeypatch.setattr(KimiHandler, "_latest_session_id", lambda self, cwd: SESSION)
    seen: list[str] = []

    result = KimiHandler().dispatch(prompt="p", cwd=tmp_path, on_session_started=seen.append)

    assert result.agent_session_id == SESSION
    assert seen == [SESSION]


def test_session_listing_uses_kimis_work_dir_field(monkeypatch, tmp_path) -> None:
    class _Completed:
        returncode = 0
        stdout = json.dumps([{"id": SESSION, "workDir": str(tmp_path)}])

    monkeypatch.setattr(
        "playmaker.agents.kimi.subprocess.run", lambda *args, **kwargs: _Completed()
    )

    assert KimiHandler()._latest_session_id(tmp_path) == SESSION


def test_session_listing_sorts_matching_entries_by_updated_at(monkeypatch, tmp_path) -> None:
    class _Completed:
        returncode = 0
        stdout = json.dumps(
            [
                {"id": SESSION, "workDir": str(tmp_path), "updatedAt": 100},
                {"id": NEWER_SESSION, "workDir": str(tmp_path), "updatedAt": 200},
            ]
        )

    monkeypatch.setattr(
        "playmaker.agents.kimi.subprocess.run", lambda *args, **kwargs: _Completed()
    )

    assert KimiHandler()._latest_session_id(tmp_path) == NEWER_SESSION


def test_nonzero_exit_surfaces_stderr(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(
        _events({"role": "meta", "type": "system.version", "version": "0.41.0"}),
        stderr='error: failed to run prompt: Model "kimi-code/nope" is not configured',
        returncode=1,
    )
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="kimi-code/nope"):
        KimiHandler().dispatch(prompt="p", cwd=tmp_path)


def test_large_stderr_is_drained_while_stdout_is_streamed(monkeypatch, tmp_path) -> None:
    fake = _ConcurrentStderrPopen(
        _events({"role": "meta", "type": "system.version", "version": "0.41.0"}),
        "x" * 70_000 + " stderr detail",
    )
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError) as exc_info:
        KimiHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "stderr detail" in str(exc_info.value)
    assert fake.stderr_started.is_set()


def test_explicit_model_wins_and_prompt_mode_never_passes_approval_flags(
    monkeypatch, tmp_path, configured
) -> None:
    configured({"agents": {"kimi": {"model": "kimi-code/configured"}}})
    fake = _FakePopen(_events(_assistant("DONE"), _resume_hint()))
    _install(monkeypatch, fake)

    KimiHandler().dispatch(prompt="p", cwd=tmp_path, model="kimi-code/explicit")

    assert fake.cmd[fake.cmd.index("-m") + 1] == "kimi-code/explicit"
    assert fake.cmd[fake.cmd.index("-p") + 1] == "p"
    assert fake.cmd[fake.cmd.index("--output-format") + 1] == "stream-json"
    assert not {"--auto", "--yolo", "--plan"} & set(fake.cmd)


def test_configured_model_is_used_when_no_model_is_passed(
    monkeypatch, tmp_path, configured
) -> None:
    configured({"agents": {"kimi": {"model": "kimi-code/configured"}}})
    fake = _FakePopen(_events(_assistant("DONE"), _resume_hint()))
    _install(monkeypatch, fake)

    KimiHandler().dispatch(prompt="p", cwd=tmp_path)

    assert fake.cmd[fake.cmd.index("-m") + 1] == "kimi-code/configured"


def test_no_model_flag_when_neither_dispatch_nor_config_supplies_one(
    monkeypatch, tmp_path, configured
) -> None:
    configured({})
    fake = _FakePopen(_events(_assistant("DONE"), _resume_hint()))
    _install(monkeypatch, fake)

    KimiHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "-m" not in fake.cmd


def test_resume_uses_dash_s_and_keeps_the_session_id(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(_events(_assistant("DONE2"), _resume_hint()))
    _install(monkeypatch, fake)

    result = KimiHandler().resume(prompt="more", cwd=tmp_path, agent_session_id=SESSION)

    assert fake.cmd[fake.cmd.index("-S") + 1] == SESSION
    assert result.agent_session_id == SESSION


def test_find_session_file_verifies_the_state_id_and_cwd(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    home = Path(os.environ["KIMI_CODE_HOME"])
    session_dir = home / "sessions" / "wd_workspace_0123456789ab" / SESSION
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text("", encoding="utf-8")
    (session_dir / "state.json").write_text(
        json.dumps({"id": SESSION, "cwd": str(cwd.resolve())}), encoding="utf-8"
    )

    assert KimiHandler().find_session_file(SESSION, cwd) == wire


def test_find_session_file_without_state_uses_the_workspace_directory_name(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    home = Path(os.environ["KIMI_CODE_HOME"])

    def make_wire(workspace_name: str) -> Path:
        wire = home / "sessions" / workspace_name / SESSION / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.write_text("", encoding="utf-8")
        return wire

    matching_wire = make_wire("wd_workspace_0123456789ab")
    make_wire("wd_other_abcdef012345")

    handler = KimiHandler()
    assert handler.find_session_file(SESSION, cwd) == matching_wire
    assert handler.find_session_file(SESSION, tmp_path / "unrelated") is None


def test_parse_session_file_maps_user_text_tools_and_assistant_text() -> None:
    turns = KimiHandler().parse_session_file(FIXTURE)

    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert turns[0].content.startswith("Create a file named hello.txt")
    assert turns[0].timestamp is not None
    assert turns[1].content == "DONE"
    assert turns[1].tool_calls == [
        {
            "id": "tool_b9dutdcTH3OmrVAuOyVX7lsg",
            "name": "Write",
            "input": {"path": "hello.txt", "content": "OK"},
        }
    ]
    assert turns[1].tool_results == [
        {"tool_use_id": "tool_b9dutdcTH3OmrVAuOyVX7lsg", "content": "Wrote 2 bytes to hello.txt"}
    ]


def test_registry_exposes_kimi() -> None:
    assert get_handler("kimi").name == "kimi"
    assert "kimi" in all_handlers()

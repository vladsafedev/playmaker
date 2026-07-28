"""opencode handler: stream dispatch, model routing, and transcript parsing."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.agents.opencode as opencode
import playmaker.config as config
from playmaker.agents.opencode import OpencodeHandler


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> str:
        return "".join(self._lines)


class _FakePopen:
    """Stand-in for subprocess.Popen over `opencode run --format json`."""

    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self._lines = lines
        self.returncode = returncode
        self.cmd: list[str] = []
        self.kwargs: dict = {}
        self.stdout = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout = _FakeStream(self._lines)
        return self

    def wait(self) -> int:
        return self.returncode


def _events(*objs: dict) -> list[str]:
    return [json.dumps(o) + "\n" for o in objs]


SESSION = "ses_3f27c7794ffebakQutp5kQmZsP"

STEP_START = {
    "type": "step_start",
    "sessionID": SESSION,
    "part": {"id": "prt_1", "messageID": "msg_1", "type": "step-start"},
}


def _text(part_id: str, text: str) -> dict:
    return {
        "type": "text",
        "sessionID": SESSION,
        "part": {"id": part_id, "messageID": "msg_1", "type": "text", "text": text},
    }


def _step_finish(cost: float) -> dict:
    return {
        "type": "step_finish",
        "sessionID": SESSION,
        "part": {"id": "prt_9", "type": "step-finish", "reason": "stop", "cost": cost},
    }


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    """Keep tests off the real ~/.local/share/opencode and ~/.playmaker."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(opencode, "PLAYMAKER_HOME", tmp_path / "playmaker")


def _make_db(tmp_path: Path, *, cost: float | None = None,
             messages: list[tuple[str, int, dict]] | None = None,
             parts: list[tuple[str, str, int, dict]] | None = None) -> Path:
    """A minimal stand-in for opencode.db — the columns the handler reads."""
    db = tmp_path / "xdg" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, cost REAL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
                              time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                           time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session VALUES (?, ?, ?)", (SESSION, "/tmp/wherever", cost))
    for message_id, created, data in messages or []:
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (message_id, SESSION, created, json.dumps(data)),
        )
    for part_id, message_id, created, data in parts or []:
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (part_id, message_id, SESSION, created, json.dumps(data)),
        )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def configured(monkeypatch):
    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


@pytest.fixture(autouse=True)
def no_roster(monkeypatch):
    """Default to an unreadable roster so validation stays out of the way."""
    monkeypatch.setattr(OpencodeHandler, "available_models", staticmethod(tuple))


def _install(monkeypatch, fake: _FakePopen) -> None:
    monkeypatch.setattr("playmaker.agents.opencode.subprocess.Popen", fake)


# ---- dispatch ---------------------------------------------------------------


def test_dispatch_returns_the_last_text_and_the_session_id(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(
        _events(
            STEP_START,
            _text("prt_2", "thinking out loud"),
            _text("prt_3", "DONE"),
            _step_finish(0.0042),
        )
    )
    _install(monkeypatch, fake)

    result = OpencodeHandler().dispatch(prompt="do it", cwd=tmp_path)

    assert result.agent_session_id == SESSION
    assert result.initial_output == "DONE"
    assert result.cost_usd == pytest.approx(0.0042)
    assert result.exit_code == 0


def test_dispatch_fires_the_session_callback_on_the_first_event(monkeypatch, tmp_path) -> None:
    # Every opencode event carries sessionID, so a detached dispatch records the
    # id from the first line rather than waiting for the run to finish.
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok"), _step_finish(0.0)))
    _install(monkeypatch, fake)
    seen: list[str] = []

    OpencodeHandler().dispatch(prompt="p", cwd=tmp_path, on_session_started=seen.append)

    assert seen == [SESSION]


def test_a_restreamed_text_part_is_not_duplicated(monkeypatch, tmp_path) -> None:
    # The same part id is re-emitted as it streams; the later event is fuller.
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "half"), _text("prt_2", "half and half")))
    _install(monkeypatch, fake)

    result = OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert result.initial_output == "half and half"


def test_dispatch_survives_a_stream_cut_short_before_step_finish(monkeypatch, tmp_path) -> None:
    # opencode#26855: the json stream can exit before the final step_finish.
    # The run still succeeded, so we report it — just without a cost.
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "partial answer")))
    _install(monkeypatch, fake)

    result = OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert result.initial_output == "partial answer"
    assert result.cost_usd is None


def test_cost_prefers_opencodes_own_accounting_over_the_stream(monkeypatch, tmp_path) -> None:
    # The session row is authoritative when the stream undercounts.
    _make_db(tmp_path, cost=0.12)
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "hi"), _step_finish(0.001)))
    _install(monkeypatch, fake)

    result = OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert result.cost_usd == pytest.approx(0.12)


def test_a_failed_run_surfaces_the_stream_error(monkeypatch, tmp_path) -> None:
    fake = _FakePopen(
        _events(
            STEP_START,
            {
                "type": "error",
                "sessionID": SESSION,
                "error": {"name": "ProviderError", "data": {"message": "rate limited"}},
            },
        ),
        returncode=1,
    )
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="rate limited"):
        OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)


def test_a_run_with_no_session_id_is_an_error(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, _FakePopen(_events({"type": "noise"})))

    with pytest.raises(RuntimeError, match="no sessionID"):
        OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)


# ---- model routing ----------------------------------------------------------


def test_the_configured_model_is_used_when_none_is_given(monkeypatch, tmp_path, configured) -> None:
    # Without this, opencode falls back to whatever its own opencode.json says,
    # which is usually the model the user last picked interactively.
    configured({"agents": {"opencode": {"model": "zai-coding-plan/glm-5.2"}}})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)

    OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert fake.cmd[fake.cmd.index("-m") + 1] == "zai-coding-plan/glm-5.2"


def test_an_explicit_model_beats_the_configured_one(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"opencode": {"model": "zai-coding-plan/glm-5.2"}}})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)

    OpencodeHandler().dispatch(prompt="p", cwd=tmp_path, model="lmstudio/qwen/qwen3-coder-30b")

    assert fake.cmd[fake.cmd.index("-m") + 1] == "lmstudio/qwen/qwen3-coder-30b"


def test_no_model_flag_when_nothing_is_configured(monkeypatch, tmp_path, configured) -> None:
    configured({})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)

    OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "-m" not in fake.cmd


def test_an_unknown_model_is_rejected_with_the_roster(monkeypatch) -> None:
    monkeypatch.setattr(
        OpencodeHandler,
        "available_models",
        staticmethod(lambda: ("zai-coding-plan/glm-5.2", "lmstudio/qwen/qwen3-coder-30b")),
    )
    handler = OpencodeHandler()

    with pytest.raises(RuntimeError, match="zai-coding-plan/glm-5.2"):
        handler._validate_model("zai-coding-plan/glm-9000")


def test_an_unreadable_roster_does_not_block_a_dispatch() -> None:
    # A probe failure must not be the reason a dispatch never happens.
    OpencodeHandler()._validate_model("anything/at-all")


# ---- flags ------------------------------------------------------------------


def test_auto_is_passed_by_default(monkeypatch, tmp_path, configured) -> None:
    # opencode has no middle permission tier, so a detached run without --auto
    # can come back having done nothing.
    configured({})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)

    OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "--auto" in fake.cmd


def test_auto_can_be_turned_off(monkeypatch, tmp_path, configured) -> None:
    configured({"agents": {"opencode": {"yolo": False}}})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)

    OpencodeHandler().dispatch(prompt="p", cwd=tmp_path)

    assert "--auto" not in fake.cmd


def test_the_prompt_is_the_last_argument(monkeypatch, tmp_path, configured) -> None:
    # `-f` is a yargs array flag and `message` is an array positional, so the
    # prompt has to stay the sole trailing positional; file refs go inline.
    configured({})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)

    OpencodeHandler().dispatch(
        prompt="review this", cwd=tmp_path, files=[Path("a.py"), Path("b.py")]
    )

    assert fake.cmd[-1] == "review this\n\n@a.py @b.py"


def test_the_working_directory_is_pinned_by_dir_and_pwd(monkeypatch, tmp_path, configured) -> None:
    # opencode resolves its cwd from process.env.PWD, which Popen(cwd=…) leaves
    # pointing at the parent — without both of these a dispatch writes its files
    # into whatever directory the coach happened to be sitting in.
    configured({})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "ok")))
    _install(monkeypatch, fake)
    target = tmp_path / "workspace"
    target.mkdir()

    OpencodeHandler().dispatch(prompt="p", cwd=target)

    assert fake.cmd[fake.cmd.index("--dir") + 1] == str(target)
    assert fake.kwargs["cwd"] == str(target)
    assert fake.kwargs["env"]["PWD"] == str(target)


def test_resume_continues_the_same_session(monkeypatch, tmp_path, configured) -> None:
    configured({})
    fake = _FakePopen(_events(STEP_START, _text("prt_2", "resumed")))
    _install(monkeypatch, fake)

    result = OpencodeHandler().resume(prompt="more", cwd=tmp_path, agent_session_id=SESSION)

    assert fake.cmd[fake.cmd.index("-s") + 1] == SESSION
    assert result.agent_session_id == SESSION


# ---- transcript -------------------------------------------------------------


def test_parse_session_file_orders_turns_and_maps_roles(tmp_path) -> None:
    _make_db(
        tmp_path,
        messages=[
            ("msg_2", 2000, {"role": "assistant", "time": {"created": 2000}}),
            ("msg_1", 1000, {"role": "user", "time": {"created": 1000}}),
        ],
        parts=[
            ("prt_a", "msg_1", 1000, {"type": "text", "text": "fix the bug"}),
            ("prt_b", "msg_2", 2001, {"type": "step-start"}),
            ("prt_c", "msg_2", 2002, {"type": "reasoning", "text": "let me look"}),
            ("prt_d", "msg_2", 2003, {"type": "text", "text": "fixed it"}),
        ],
    )
    pointer = OpencodeHandler().find_session_file(SESSION, tmp_path)

    turns = OpencodeHandler().parse_session_file(pointer)

    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].content == "fix the bug"
    assert turns[1].content == "[thinking] let me look\nfixed it"
    assert turns[0].timestamp is not None


def test_parse_session_file_pairs_tool_calls_with_their_results(tmp_path) -> None:
    _make_db(
        tmp_path,
        messages=[("msg_1", 1, {"role": "assistant", "time": {"created": 1}})],
        parts=[
            (
                "prt_a",
                "msg_1",
                2,
                {
                    "type": "tool",
                    "callID": "call_42",
                    "tool": "glob",
                    "state": {
                        "status": "completed",
                        "input": {"pattern": "*.py"},
                        "output": "a.py\nb.py",
                    },
                },
            )
        ],
    )
    pointer = OpencodeHandler().find_session_file(SESSION, tmp_path)

    (turn,) = OpencodeHandler().parse_session_file(pointer)

    assert turn.tool_calls == [
        {"id": "call_42", "name": "glob", "input": {"pattern": "*.py"}}
    ]
    assert turn.tool_results == [{"tool_use_id": "call_42", "content": "a.py\nb.py"}]


def test_parse_session_file_tolerates_a_missing_database(tmp_path) -> None:
    # opencode's schema is internal; a reshape must degrade, not raise.
    assert OpencodeHandler().parse_session_file(tmp_path / f"{SESSION}.session") == []


def test_find_session_file_writes_a_pointer_named_for_the_session(tmp_path) -> None:
    # The transcript is in SQLite, but `thread --follow` needs a path that
    # exists, so the pointer stands in for it and carries the id.
    _make_db(tmp_path)

    pointer = OpencodeHandler().find_session_file(SESSION, tmp_path)

    assert pointer.exists()
    assert pointer.stem == SESSION
    assert SESSION in pointer.read_text()


def test_find_session_file_returns_none_for_an_unknown_session(tmp_path) -> None:
    _make_db(tmp_path)

    assert OpencodeHandler().find_session_file("ses_nope", tmp_path) is None


def test_find_session_file_returns_none_without_a_database(tmp_path) -> None:
    assert OpencodeHandler().find_session_file(SESSION, tmp_path) is None

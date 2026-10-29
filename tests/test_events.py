"""The event stream: status/batch events, the `events` command, failure reasons."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.state as state
from playmaker import cli
from playmaker import events as events_mod
from playmaker.agents.base import DispatchResult

runner = CliRunner()


@pytest.fixture
def db(monkeypatch, tmp_path: Path) -> Path:
    """Point module-level state paths at a disposable home."""
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()
    return home


class _Worker:
    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model):
        return self._finish(on_session_started)

    def resume(self, prompt, cwd, agent_session_id, files, *, on_session_started, model):
        return self._finish(on_session_started)

    def _finish(self, on_session_started) -> DispatchResult:
        on_session_started("fake-agent-session")
        return DispatchResult(
            agent_session_id="fake-agent-session",
            cwd="",
            session_file=None,
            initial_output="worker finished",
        )


class _FailingWorker:
    def __init__(self, message: str) -> None:
        self.message = message

    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model):
        raise RuntimeError(self.message)

    def resume(self, prompt, cwd, agent_session_id, files, *, on_session_started, model):
        raise RuntimeError(self.message)


def _latest_session() -> dict:
    return state.list_sessions(limit=1)[0]


def _events_output(*args: str) -> list[dict]:
    result = runner.invoke(cli.app, ["events", *args])
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.output.splitlines() if line.strip()]


def test_running_then_done_emits_status_events(db: Path, monkeypatch, tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "Read only, answer the question.",
            "--cwd",
            str(cwd),
            "--sync",
        ],
    )

    assert result.exit_code == 0, result.output
    sid = _latest_session()["id"]
    transitions = [
        (e["prev_status"], e["status"])
        for e in _events_output("--session", sid, "--kind", "status")
    ]
    assert transitions == [("pending", "running"), ("running", "done")]
    status_events = _events_output("--session", sid)
    assert all(e["session_id"] == sid and e["agent"] == "fake" for e in status_events)
    assert all(e["detail"] is None for e in status_events)


def test_failed_run_carries_reason_in_column_event_and_get(db, monkeypatch, tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("rate limit exceeded"))
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt", "Write a file.", "--cwd", str(cwd), "--sync"],
    )

    assert result.exit_code == 1
    row = _latest_session()
    assert row["status"] == "failed"
    assert row["fail_reason"] == "quota"
    failed_events = _events_output("--session", row["id"], "--kind", "status")
    assert failed_events[-1]["status"] == "failed"
    assert failed_events[-1]["prev_status"] == "running"
    assert failed_events[-1]["detail"] == {"reason": "quota"}
    get_result = runner.invoke(cli.app, ["get", row["id"], "--json"])
    assert get_result.exit_code == 0
    assert json.loads(get_result.output)["fail_reason"] == "quota"
    assert "quota" in runner.invoke(cli.app, ["get", row["id"]]).output


def test_log_tail_contributes_to_the_classification(db, monkeypatch, tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(cli, "get_handler", lambda name: _FailingWorker("boom"))
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    sid = state.insert_session(agent="fake", prompt="p", cwd=str(cwd))
    state.update_session(sid, status="running", pid=1)
    (state.LOGS_DIR / f"{sid}.log").write_text(
        "agent stderr:\nYour organization has reached its usage limit", encoding="utf-8"
    )

    result = runner.invoke(cli.app, ["_run-detached", sid])

    assert result.exit_code == 1
    assert state.get_session(sid)["fail_reason"] == "quota"


def test_batch_drain_appends_a_batch_event(db: Path, monkeypatch, tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)

    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "Read only, answer the question.",
            "--cwd",
            str(cwd),
            "--batch",
            "fanout",
            "--sync",
        ],
    )

    assert result.exit_code == 0, result.output
    batch_events = [e for e in _events_output("--batch", "fanout") if e["kind"] == "batch"]
    assert len(batch_events) == 1
    event = batch_events[0]
    assert event["session_id"] is None
    assert event["batch_id"] == "fanout"
    assert event["status"] == "done"
    assert event["detail"] == {"total": 1, "done": 1, "failed": 0, "no_changes": 0}
    statuses = [
        e["status"] for e in _events_output("--batch", "fanout", "--kind", "status")
    ]
    assert statuses == ["running", "done"]


def test_events_last_keeps_the_newest_matching_rows(db: Path) -> None:
    first = state.insert_session(agent="codex", prompt="a", cwd="/r", batch_id="b")
    second = state.insert_session(agent="agy", prompt="b", cwd="/r", batch_id="b")
    state.update_session(first, status="running")
    state.update_session(first, status="done")
    state.update_session(second, status="running")

    default_rows = _events_output()
    assert len(default_rows) == 3  # the non-follow default is the last 50
    last_two = _events_output("--last", "2")
    assert [e["session_id"] for e in last_two] == [first, second]
    assert all(e["kind"] == "status" for e in last_two)
    assert _events_output("--last", "0") == []


def test_events_since_filters_by_timestamp(db: Path) -> None:
    sid = state.insert_session(agent="codex", prompt="a", cwd="/r")
    state.update_session(sid, status="running")
    state.update_session(sid, status="done")

    past = _events_output("--since", "2020-01-01T00:00:00+00:00")
    assert [e["status"] for e in past] == ["running", "done"]
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _events_output("--since", future) == []
    bad = runner.invoke(cli.app, ["events", "--since", "not-a-date"])
    assert bad.exit_code == 2


def test_events_follow_prints_only_new_events_and_stops_cleanly(db, monkeypatch) -> None:
    sid = state.insert_session(agent="codex", prompt="a", cwd="/r")
    state.update_session(sid, status="running")
    other = state.insert_session(agent="agy", prompt="b", cwd="/r")
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            state.update_session(other, status="running")  # a new event arrives
            return
        raise KeyboardInterrupt  # the user hits Ctrl-C

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    result = runner.invoke(cli.app, ["events", "--follow", "--interval", "1"])

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    # Default --follow prints nothing up front: only the event that arrived
    # after startup, not the pre-existing running transition.
    assert [e["session_id"] for e in lines] == [other]
    assert lines[0]["kind"] == "status"


def test_events_follow_prints_an_event_landed_between_cursor_and_listing(db, monkeypatch) -> None:
    sid = state.insert_session(agent="codex", prompt="a", cwd="/r")
    state.update_session(sid, status="running")
    real_list_events = events_mod.list_events
    calls = {"n": 0}

    def racing_list_events(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the initial listing: an event lands mid-read
            state.update_session(sid, status="done")
        return real_list_events(**kwargs)

    monkeypatch.setattr(events_mod, "list_events", racing_list_events)
    sleeps = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise KeyboardInterrupt  # one poll, then the user hits Ctrl-C

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    result = runner.invoke(cli.app, ["events", "--follow", "--interval", "1"])

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    # The racing event sits above the cursor but outside the (empty, --last 0)
    # initial listing — the first poll must still deliver it, exactly once.
    assert len(lines) == 1
    assert lines[0]["session_id"] == sid
    assert lines[0]["status"] == "done"


def test_events_follow_rejects_a_non_positive_interval(db: Path) -> None:
    for bad in ("0", "-1.5"):
        result = runner.invoke(cli.app, ["events", "--follow", "--interval", bad])
        assert result.exit_code == 2


def test_events_without_follow_accepts_a_zero_interval(db: Path) -> None:
    sid = state.insert_session(agent="codex", prompt="a", cwd="/r", batch_id="b")
    state.update_session(sid, status="running")

    result = runner.invoke(cli.app, ["events", "--batch", "b", "--interval", "0"])

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert [e["status"] for e in lines] == ["running"]


def test_events_follow_last_n_prints_ascending_ids_across_the_race(db, monkeypatch) -> None:
    sid = state.insert_session(agent="codex", prompt="a", cwd="/r")
    for n in range(100):
        state.add_event(sid, "status", status=f"s{n}")  # ids 1..100
    real_list_events = events_mod.list_events
    calls = {"n": 0}

    def racing_list_events(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the cursor is already captured at 100
            for n in range(20):  # ids 101..120 land before the listing returns
                state.add_event(sid, "status", status=f"r{n}")
        return real_list_events(**kwargs)

    monkeypatch.setattr(events_mod, "list_events", racing_list_events)
    sleeps = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise KeyboardInterrupt  # one poll, then the user hits Ctrl-C

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    result = runner.invoke(cli.app, ["events", "--follow", "--last", "5", "--interval", "1"])

    assert result.exit_code == 0, result.output
    ids = [
        json.loads(line)["id"] for line in result.output.splitlines() if line.strip()
    ]
    # --last 5 shows the newest rows at or below the cursor (96..100); the
    # racing 101..120 arrive on the first poll — never interleaved backwards.
    assert ids == list(range(96, 121))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Usage limit reached — upgrade your plan", "quota"),
        ("monthly quota exhausted", "quota"),
        ("HTTP 429: too many requests", "quota"),
        ("rate-limit exceeded for model", "quota"),
        # The Console API key with an empty balance (claude-api lane): HTTP 400, not 429.
        ("Your credit balance is too low to access the Anthropic API.", "quota"),
        ("HTTP 400: insufficient credits", "quota"),
        ("You've hit your limit for now", "unknown"),  # not a listed quota phrase
        ("401 unauthorized", "auth"),
        ("HTTP 403", "auth"),
        ("token expired, refresh it", "auth"),
        ("please login to continue", "auth"),
        ("OAuth consent was revoked", "auth"),
        ("codex: command not found", "binary"),
        ("No such file or directory (errno 2)", "binary"),
        ("spawn agy ENOENT", "binary"),
        ("unknown model: fake-model-9", "model"),
        ("invalid model specified", "model"),
        ("Error: model 'gpt-6' not found", "model"),
        ("TypeError: 'NoneType' object is not iterable", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_failure_table(text: str, expected: str) -> None:
    assert events_mod.classify_failure(text) == expected


def test_classify_failure_prefers_quota_over_auth_and_combines_texts() -> None:
    assert events_mod.classify_failure("HTTP 429 unauthorized") == "quota"
    assert events_mod.classify_failure("boom", "", "we also saw a login error") == "auth"
    assert events_mod.classify_failure() == "unknown"


def test_read_log_tail_returns_the_last_4k_and_tolerates_missing_files(
    db: Path, tmp_path: Path
) -> None:
    log = state.LOGS_DIR / "session.log"
    log.write_text("A" * 5000 + "B" * 100, encoding="utf-8")

    tail = events_mod.read_log_tail(log)

    assert len(tail) == 4096
    assert tail.endswith("B" * 100)
    assert events_mod.read_log_tail(tmp_path / "nope.log") == ""

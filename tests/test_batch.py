"""One `--batch` label, many fan-outs: which sessions each summary covers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.state as state
from playmaker import cli


@pytest.fixture
def db(monkeypatch, tmp_path: Path):
    """Point the module-level paths at a throwaway home and initialise it."""
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()
    return home


@pytest.fixture
def summaries(monkeypatch) -> list[str]:
    """Capture the message of every notification the batch code fires."""
    fired: list[str] = []
    monkeypatch.setattr(
        cli.notify, "notify", lambda title, message, **kwargs: fired.append(message)
    )
    return fired


def _finish(session_id: str, status: str = "done") -> None:
    state.update_session(session_id, status=status, finished_at=state.now_iso())


def _fan_out(label: str, agents: list[str]) -> list[str]:
    """Dispatch a batch, then let each member finish in order."""
    ids = [state.insert_session(agent=a, prompt="p", cwd="/repo", batch_id=label) for a in agents]
    for sid in ids:
        _finish(sid)
        cli._maybe_finalize_batch(label)
    return ids


def test_the_summary_waits_for_the_whole_fan_out(db: Path, summaries: list[str]) -> None:
    first = state.insert_session(agent="codex", prompt="p", cwd="/repo", batch_id="dash")
    state.insert_session(agent="claude", prompt="p", cwd="/repo", batch_id="dash")

    _finish(first)
    cli._maybe_finalize_batch("dash")

    assert summaries == []


def test_the_summary_fires_exactly_once_per_fan_out(db: Path, summaries: list[str]) -> None:
    # Every detached dispatch calls the finaliser as it lands, so the last two
    # to finish can both see an all-terminal batch.
    _fan_out("dash", ["codex", "claude"])

    cli._maybe_finalize_batch("dash")

    assert summaries == ["2/2 done · codex ✓ · claude ✓"]


def test_a_reused_batch_label_summarises_each_fan_out_separately(
    db: Path, summaries: list[str]
) -> None:
    # The README teaches `B=dashboard` as a shell variable you keep around, so
    # the same label comes back tomorrow with a different set of agents.
    _fan_out("dash", ["codex", "claude"])

    _fan_out("dash", ["agy", "opencode"])

    assert summaries == [
        "2/2 done · codex ✓ · claude ✓",
        "2/2 done · agy ✓ · opencode ✓",
    ]


def test_killing_the_last_running_member_still_drains_the_batch(
    db: Path, summaries: list[str], monkeypatch
) -> None:
    # `kill` is a terminal state like any other; if it does not finalise, the
    # batch it emptied never reports at all.
    done = state.insert_session(agent="codex", prompt="p", cwd="/repo", batch_id="dash")
    doomed = state.insert_session(agent="claude", prompt="p", cwd="/repo", batch_id="dash")
    _finish(done)
    cli._maybe_finalize_batch("dash")
    state.update_session(doomed, status="running", pid=4242)
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: None)

    cli.kill(doomed)

    assert summaries == ["1/2 done · codex ✓ · claude ✗"]


def test_the_combined_batch_file_lands_next_to_the_outputs_it_quotes(db: Path, monkeypatch) -> None:
    # It is the file the notification click opens, so it belongs with the rest
    # of a run's artefacts rather than in a world-writable /tmp under a name
    # anyone can predict — and a hard-coded /tmp escapes tmp_path in tests.
    opened: list[str | None] = []
    monkeypatch.setattr(
        cli.notify,
        "notify",
        lambda title, message, **kwargs: opened.append(kwargs.get("open_path")),
    )

    _fan_out("dash", ["codex", "claude"])

    combined = db / "outputs" / "batch-dash.md"
    assert opened == [str(combined)]
    assert combined.read_text(encoding="utf-8").count("\n## ") == 2


def test_a_batch_of_one_that_failed_reports_the_failure(db: Path, summaries: list[str]) -> None:
    sid = state.insert_session(agent="agy", prompt="p", cwd="/repo", batch_id="solo")

    _finish(sid, status="failed")
    cli._maybe_finalize_batch("solo")

    assert summaries == ["0/1 done · agy ✗"]

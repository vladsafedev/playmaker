"""Queue persistence and chained delivery."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from playmaker import cli, config, queue, state
from playmaker.agents.base import DispatchResult

runner = CliRunner()


@pytest.fixture
def db(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()


def _root(tmp_path: Path) -> str:
    sid = state.insert_session(agent="fake", prompt="root", cwd=str(tmp_path))
    state.update_session(sid, status="done", agent_session_id="agent-thread")
    return sid


class _Worker:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model, **kwargs):
        return self._run(cwd, on_session_started)

    def resume(
        self, prompt, cwd, agent_session_id, files, *, on_session_started, model, **kwargs
    ):
        return self._run(cwd, on_session_started)

    def _run(self, cwd, on_session_started):
        on_session_started("agent-thread")
        if self.fail:
            raise RuntimeError("worker failed")
        return DispatchResult(
            agent_session_id="agent-thread",
            cwd=str(cwd),
            session_file=None,
            initial_output="finished",
        )


def _spawn_recorder(monkeypatch):
    spawned: list[str] = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda sid: spawned.append(sid) or 123)
    return spawned


def test_queue_delivery_chains_and_renumbers(db: None, tmp_path: Path) -> None:
    root = _root(tmp_path)
    one = queue.enqueue(root, "one")
    two = queue.enqueue(root, "two")
    queue.move(two["id"], 1)
    assert [x["prompt"] for x in queue.list_items(root)] == ["two", "one"]

    spawned: list[str] = []
    child = queue.deliver_next(root, root, lambda sid: spawned.append(sid) or 1)
    assert child == spawned[0]
    assert queue.get(two["id"])["status"] == "delivered"
    child_row = state.get_session(child)
    assert child_row and child_row["parent_id"] == root
    assert child_row["agent_session_id"] == "agent-thread"

    state.update_session(child, status="done", agent_session_id="agent-thread")
    grandchild = queue.deliver_next(root, child, lambda sid: spawned.append(sid) or 1)
    assert grandchild
    assert queue.get(one["id"])["status"] == "delivered"


def test_cancelled_and_delivered_items_are_immutable(db: None, tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = queue.enqueue(root, "later")
    queue.cancel(item["id"])
    with pytest.raises(ValueError, match="immutable"):
        queue.edit(item["id"], prompt="never")

    delivered = queue.enqueue(root, "now")
    queue.deliver_next(root, root, lambda sid: 1)
    with pytest.raises(ValueError, match="immutable"):
        queue.move(delivered["id"], 1)


def test_drain_requires_success_unless_forced(db: None, tmp_path: Path) -> None:
    root = _root(tmp_path)
    state.update_session(root, status="failed")
    queue.enqueue(root, "retry")
    spawned: list[str] = []
    assert queue.drain_idle_threads(lambda sid: spawned.append(sid) or 1) == []
    assert queue.drain_idle_threads(lambda sid: spawned.append(sid) or 1, force=True) == spawned


def test_continue_queues_live_and_resumes_idle_after_and_now(
    db: None, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    spawned = _spawn_recorder(monkeypatch)
    root = _root(tmp_path)
    state.update_session(root, status="running")
    queued = runner.invoke(cli.app, ["continue", root, "-p", "later", "--json"])
    assert queued.exit_code == 0, queued.output
    assert json.loads(queued.output)["queued"] is True
    assert queue.get(1)["cwd"] is None
    assert runner.invoke(cli.app, ["continue", root, "-p", "bad", "--sync"]).exit_code == 2
    assert (
        runner.invoke(cli.app, ["continue", root, "-p", "bad", "--after", "--now"]).exit_code == 2
    )
    now = runner.invoke(cli.app, ["continue", root, "-p", "now", "--now"])
    assert now.exit_code == 0, now.output
    assert spawned

    state.update_session(root, status="done")
    state.update_session(spawned[0], status="done", agent_session_id="agent-thread")
    idle = runner.invoke(cli.app, ["continue", root, "-p", "idle"])
    assert idle.exit_code == 0, idle.output
    assert len(spawned) == 2
    after = runner.invoke(
        cli.app, ["continue", root, "-p", "after", "--after", "--cwd", str(tmp_path)]
    )
    assert after.exit_code == 0, after.output
    assert queue.list_items(root)[-1]["cwd"] == str(tmp_path.resolve())


def test_dispatch_delivery_chain_failure_and_drain(db: None, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    spawned = _spawn_recorder(monkeypatch)
    root = state.insert_session(
        agent="fake",
        prompt="root",
        cwd=str(tmp_path),
        batch_id="batch",
        worktree_path=str(tmp_path),
        worktree_branch="wp/queue",
        repo_root=str(tmp_path),
    )
    queue.enqueue(root, "one")
    queue.enqueue(root, "two")
    cli._run_dispatch(root)
    assert len(spawned) == 1
    first = state.get_session(spawned[0])
    assert first and first["parent_id"] == root and first["agent_session_id"] == "agent-thread"
    assert first["worktree_path"] == str(tmp_path)
    assert first["worktree_branch"] == "wp/queue" and first["repo_root"] == str(tmp_path)
    assert first["batch_id"] == "batch"
    cli._run_dispatch(first["id"])
    assert len(spawned) == 2

    failed = _root(tmp_path)
    state.update_session(failed, status="pending")
    queue.enqueue(failed, "held")
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker(fail=True))
    with pytest.raises(typer.Exit):
        cli._run_dispatch(failed)
    assert queue.queued_count(failed) == 1
    with state.connect() as c:
        events = c.execute("SELECT kind FROM events WHERE session_id=?", (failed,)).fetchall()
    assert "queue_blocked" in [event["kind"] for event in events]
    assert queue.drain_idle_threads(lambda sid: 1) == []
    assert queue.drain_idle_threads(lambda sid: 1, force=True)


def test_queue_cli_mutation_json_visibility_and_refusals(
    db: None, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    spawned = _spawn_recorder(monkeypatch)
    root = _root(tmp_path)
    one = runner.invoke(cli.app, ["queue", "add", root, "-p", "one", "--json"])
    assert one.exit_code == 0
    item = json.loads(one.output)["id"]
    assert (
        runner.invoke(cli.app, ["queue", "add", root, "-p", "two", "--position", "0"]).exit_code
        == 2
    )
    assert (
        runner.invoke(cli.app, ["queue", "edit", str(item)]).output.strip() == "nothing to change"
    )
    assert runner.invoke(cli.app, ["queue", "edit", str(item), "-p", "edited"]).exit_code == 0
    assert runner.invoke(cli.app, ["queue", "move", str(item), "--to", "0"]).exit_code == 2
    assert runner.invoke(cli.app, ["get", root]).output.count("queue:    1 item(s) waiting") == 1
    assert runner.invoke(cli.app, ["queue", "show", str(item), "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["queue", "ls", "--thread", "missing"]).exit_code == 1
    assert runner.invoke(cli.app, ["queue", "drain", "--thread", "missing"]).exit_code == 1
    assert runner.invoke(cli.app, ["queue", "drain", "--json"]).exit_code == 0
    assert spawned
    assert runner.invoke(cli.app, ["queue", "rm", str(item)]).exit_code == 1

    other = queue.enqueue(root, "other")
    assert runner.invoke(cli.app, ["queue", "clear", root, "--json"]).exit_code == 0
    assert queue.get(other["id"])["status"] == "cancelled"


def test_concurrent_delivery_claims_once(db: None, tmp_path: Path) -> None:
    root = _root(tmp_path)
    queue.enqueue(root, "only")
    barrier = threading.Barrier(2)
    spawned: list[str] = []
    lock = threading.Lock()

    def spawn(sid: str) -> int:
        with lock:
            spawned.append(sid)
        return 1

    def claim() -> None:
        barrier.wait()
        queue.deliver_next(root, root, spawn)

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(spawned) == 1


def test_spawn_failure_returns_item_to_queue_and_blocks(db: None, tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = queue.enqueue(root, "retry")

    def broken_spawn(sid: str) -> int:
        raise OSError("cannot spawn")

    assert queue.deliver_next(root, root, broken_spawn) is None
    assert queue.get(item["id"])["status"] == "queued"
    children = [row for row in state.thread_sessions(root) if row["id"] != root]
    assert len(children) == 1 and children[0]["status"] == "failed"
    assert children[0]["fail_reason"] == "spawn"


def test_spawn_failure_rollback_is_one_transaction(db: None, monkeypatch, tmp_path: Path) -> None:
    root = _root(tmp_path)
    one = queue.enqueue(root, "one")
    two = queue.enqueue(root, "two")
    seen: dict[str, str] = {}
    renumber = queue._renumber

    def observing_renumber(c, thread_id: str) -> None:
        # Mid-rollback the child row is visible from the earlier claim commit,
        # so what a second connection reads here is the durable state a
        # concurrent `queue drain --force` would act on.
        with state.connect() as other:
            child = other.execute(
                "SELECT status FROM sessions WHERE parent_id=?", (root,)
            ).fetchone()
            if child is not None:
                seen["child"] = child["status"]
                seen["item"] = other.execute(
                    "SELECT status FROM queue WHERE id=?", (one["id"],)
                ).fetchone()["status"]
        renumber(c, thread_id)

    monkeypatch.setattr(queue, "_renumber", observing_renumber)

    def broken_spawn(sid: str) -> int:
        raise OSError("cannot spawn")

    assert queue.deliver_next(root, root, broken_spawn) is None
    assert seen == {"child": "pending", "item": "delivered"}
    assert queue.get(one["id"])["status"] == "queued"
    assert queue.get(one["id"])["delivered_as"] is None
    children = [row for row in state.thread_sessions(root) if row["id"] != root]
    assert len(children) == 1 and children[0]["status"] == "failed"
    assert children[0]["fail_reason"] == "spawn" and children[0]["finished_at"]

    delivered = queue.drain_idle_threads(lambda sid: 1, force=True)
    assert len(delivered) == 1
    assert queue.get(one["id"])["status"] == "delivered"
    assert queue.get(two["id"])["status"] == "queued"


def test_queue_prompt_files(db: None, tmp_path: Path) -> None:
    root = _root(tmp_path)
    source = tmp_path / "prompt.txt"
    source.write_text("from file", encoding="utf-8")
    added = runner.invoke(cli.app, ["queue", "add", root, "--prompt-file", str(source)])
    assert added.exit_code == 0, added.output
    item = queue.list_items(root)[0]
    assert item["prompt"] == "from file"
    source.write_text("edited file", encoding="utf-8")
    edited = runner.invoke(
        cli.app, ["queue", "edit", str(item["id"]), "--prompt-file", str(source)]
    )
    assert edited.exit_code == 0, edited.output
    assert queue.get(item["id"])["prompt"] == "edited file"


def test_queue_add_prepares_codex_images_before_enqueue(
    db: None, monkeypatch, tmp_path: Path
) -> None:
    root = state.insert_session(agent="codex", prompt="root", cwd=str(tmp_path))
    state.update_session(root, status="done", agent_session_id="thread-1")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    added = runner.invoke(
        cli.app, ["queue", "add", root, "-p", "look", "--files", "shot.png"]
    )

    assert added.exit_code == 0, added.output
    assert json.loads(queue.list_items(root)[0]["files"]) == [str(image)]

    missing = runner.invoke(
        cli.app, ["queue", "add", root, "-p", "missing", "--files", "absent.webp"]
    )
    assert missing.exit_code == 2
    assert "absent.webp" in missing.output
    assert queue.queued_count(root) == 1


def test_delivery_of_old_codex_item_inherits_parent_effort(db: None, tmp_path: Path) -> None:
    root = state.insert_session(
        agent="codex", prompt="root", cwd=str(tmp_path), effort="ultra"
    )
    state.update_session(root, status="done", agent_session_id="thread-1")
    queue.enqueue(root, "later")

    child = queue.deliver_next(root, root, lambda sid: 1)

    assert child is not None
    assert state.get_session(child)["effort"] == "ultra"


def test_delivery_of_old_codex_item_falls_back_to_config(
    db: None, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        config, "load_config", lambda: {"agents": {"codex": {"reasoning_effort": "high"}}}
    )
    root = state.insert_session(agent="codex", prompt="root", cwd=str(tmp_path))
    state.update_session(root, status="done", agent_session_id="thread-1")
    queue.enqueue(root, "later")

    child = queue.deliver_next(root, root, lambda sid: 1)

    assert child is not None
    assert state.get_session(child)["effort"] == "high"


def test_delivery_of_old_codex_item_drops_inherited_effort_invalid_for_its_model(
    db: None, tmp_path: Path, capsys
) -> None:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [
                            {"effort": value}
                            for value in ("low", "medium", "high", "xhigh", "max")
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    root = state.insert_session(
        agent="codex", prompt="root", cwd=str(tmp_path), effort="ultra"
    )
    state.update_session(root, status="done", agent_session_id="thread-1")
    queue.enqueue(root, "later", model="gpt-5.6-luna")

    child = queue.deliver_next(root, root, lambda sid: 1)

    assert child is not None  # an invalid inherited effort never blocks delivery
    assert state.get_session(child)["effort"] is None
    stderr = capsys.readouterr().err
    assert "gpt-5.6-luna has no reasoning effort 'ultra'" in stderr
    assert stderr.strip().count("\n") == 0  # one line


def test_queue_add_and_edit_normalize_whitespace_codex_model(db: None, tmp_path: Path) -> None:
    root = state.insert_session(
        agent="codex", prompt="root", cwd=str(tmp_path), model="gpt-6-astra"
    )
    state.update_session(root, status="done", agent_session_id="thread-1")

    added = runner.invoke(cli.app, ["queue", "add", root, "-p", "one", "--model", "  "])

    assert added.exit_code == 0, added.output
    item = queue.list_items(root)[0]
    assert item["model"] == "gpt-6-astra"  # whitespace means unset: inherit the thread's

    edited = runner.invoke(cli.app, ["queue", "edit", str(item["id"]), "--model", "  "])

    assert edited.exit_code == 0, edited.output
    assert queue.get(item["id"])["model"] == "gpt-6-astra"


def test_queue_edit_prepares_codex_images_before_edit(db: None, tmp_path: Path) -> None:
    root = state.insert_session(agent="codex", prompt="root", cwd=str(tmp_path))
    state.update_session(root, status="done", agent_session_id="thread-1")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    item = queue.enqueue(root, "look")

    edited = runner.invoke(cli.app, ["queue", "edit", str(item["id"]), "--files", "shot.png"])

    assert edited.exit_code == 0, edited.output
    assert json.loads(queue.get(item["id"])["files"]) == [str(image)]

    missing = runner.invoke(
        cli.app, ["queue", "edit", str(item["id"]), "--files", "absent.webp"]
    )
    assert missing.exit_code == 2
    assert "absent.webp" in missing.output
    assert json.loads(queue.get(item["id"])["files"]) == [str(image)]


def test_playmaker_config_is_isolated_from_the_developer_home() -> None:
    # conftest points playmaker's config at an empty temp file: a real
    # [agents.codex] reasoning_effort must never leak into a test.
    assert config.load_config() == {}


def test_init_template_carries_commented_setting_sections() -> None:
    import tomllib

    cfg = tomllib.loads(cli._DEFAULT_CONFIG)
    expected = {
        "worktree": ['setup_timeout = "10m"'],
        "gate": ['timeout = "20m"'],
        "limits": ["max_parallel = 0", "min_free_ram_gb = 0"],
        "events": [f"interval = {cli.DEFAULT_EVENTS_INTERVAL}"],
        "fallback": [f"window = {cli.DEFAULT_FALLBACK_WINDOW}"],
    }
    for section, keys in expected.items():
        assert f"# [{section}]" in cli._DEFAULT_CONFIG
        for line in keys:
            assert f"# {line}" in cli._DEFAULT_CONFIG
        assert section not in cfg  # every new line stays commented out

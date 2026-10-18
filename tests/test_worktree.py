"""Real git worktree coverage for the dispatch isolation helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from playmaker import cli, config, state, worktree
from playmaker.agents.base import DispatchResult

runner = CliRunner()


@pytest.fixture
def db(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / ".playmaker"
    monkeypatch.setattr(state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(state, "DB_PATH", home / "state.db")
    monkeypatch.setattr(state, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(state, "OUTPUTS_DIR", home / "outputs")
    monkeypatch.setattr(state, "AGENTS_DIR", home / "agents")
    state.init_db()
    return home


class _Worker:
    def __init__(self, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises

    def is_available(self) -> bool:
        return True

    def dispatch(self, prompt, cwd, files, *, on_session_started, model):
        self.calls += 1
        if self.raises:
            raise RuntimeError("worker boom")
        on_session_started("fake")
        return DispatchResult("fake", str(cwd), None, "done")

    def resume(self, *args, **kwargs):
        return self.dispatch(*args, **kwargs)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    return root


def test_no_hook_creates_then_reuses_real_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    first = worktree.prepare(root, "demo", None, tmp_path / "setup.log", 1)
    second = worktree.prepare(root, "demo", None, tmp_path / "setup.log", 1)

    assert first.path.is_dir()
    assert first == second
    assert first.branch == "wp/demo"


def test_setup_hook_receives_path_branch_and_base_and_failure_raises(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    hook = root / ".playmaker" / "wt-setup.sh"
    hook.parent.mkdir()
    capture = tmp_path / "hook-args"
    hook.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{capture}"\nexit 3\n', encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.prepare(root, "broken", "HEAD", tmp_path / "setup.log", 1)

    assert exc.value.exit_code == 3
    assert capture.read_text().splitlines()[1] == "wp/broken"


def test_hook_failure_finalizes_session_without_starting_agent(
    db: Path, monkeypatch, tmp_path: Path
) -> None:
    root = _repo(tmp_path)
    hook = root / ".playmaker" / "wt-setup.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    hook.chmod(0o755)
    worker = _Worker()
    monkeypatch.setattr(cli, "get_handler", lambda name: worker)

    result = runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt", "inspect", "--cwd", str(root), "--worktree", "bad"],
    )

    row = state.list_sessions(limit=1)[0]
    assert result.exit_code == 1
    assert row["status"] == "failed"
    assert row["fail_reason"] == "worktree_setup"
    assert worker.calls == 0
    assert "exit 9" in result.output
    assert str(state.LOGS_DIR / f"{row['id']}.log") in result.output.replace("\n", "")


@pytest.mark.parametrize("command,status,code", [("true", "pass", 0), ("false", "fail", 1)])
def test_dispatch_gate_records_result(
    db: Path, monkeypatch, tmp_path: Path, command: str, status: str, code: int
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    result = runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "inspect",
            "--cwd",
            str(root),
            "--sync",
            "--gate",
            command,
        ],
    )
    row = state.list_sessions(limit=1)[0]
    assert result.exit_code == 0
    assert (row["gate_status"], row["gate_exit_code"]) == (status, code)
    assert Path(row["gate_log_path"]).is_file()


def test_gate_timeout_and_failed_worker_skips_gate(db: Path, monkeypatch, tmp_path: Path) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker())
    monkeypatch.setattr(cli.notify, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        config, "setting", lambda section, key, default: "0.01s" if section == "gate" else default
    )
    runner.invoke(
        cli.app,
        [
            "dispatch",
            "fake",
            "--prompt",
            "inspect",
            "--cwd",
            str(root),
            "--sync",
            "--gate",
            "sleep 1",
        ],
    )
    timeout_row = state.list_sessions(limit=1)[0]
    assert (timeout_row["gate_status"], timeout_row["gate_exit_code"]) == ("timeout", 124)

    monkeypatch.setattr(cli, "get_handler", lambda name: _Worker(raises=True))
    runner.invoke(
        cli.app,
        ["dispatch", "fake", "--prompt", "inspect", "--cwd", str(root), "--sync", "--gate", "true"],
    )
    failed_row = state.list_sessions(limit=1)[0]
    assert failed_row["status"] == "failed"
    assert failed_row["gate_status"] is None


def _session_for_worktree(root: Path, name: str, status: str = "done") -> Path:
    wt = worktree.prepare(root, name, None, root / f"{name}.log", 1)
    sid = state.insert_session(
        agent="fake",
        prompt="x",
        cwd=str(wt.path),
        worktree_path=str(wt.path),
        worktree_branch=wt.branch,
        repo_root=str(root),
    )
    state.update_session(sid, status=status)
    return wt.path


def test_worktree_ls_and_rm_refusals_and_force(db: Path, tmp_path: Path) -> None:
    root = _repo(tmp_path)
    path = _session_for_worktree(root, "demo", "running")
    listed = runner.invoke(cli.app, ["worktree", "ls", "--repo", str(root), "--json"])
    item = json.loads(listed.output)[0]
    assert item["name"] == "demo" and item["branch"] == "wp/demo" and item["exists"]
    assert item["sessions"] == 1 and item["last_status"] == "running"
    assert runner.invoke(cli.app, ["worktree", "rm", "demo", "--repo", str(root)]).exit_code == 1
    with state.connect() as conn:
        conn.execute("UPDATE sessions SET status = 'done' WHERE worktree_path = ?", (str(path),))
        conn.commit()
    (path / "dirty").write_text("x", encoding="utf-8")
    assert runner.invoke(cli.app, ["worktree", "rm", "demo", "--repo", str(root)]).exit_code == 1
    assert (
        runner.invoke(cli.app, ["worktree", "rm", "demo", "--repo", str(root), "--force"]).exit_code
        == 0
    )
    assert not path.exists()


def test_prune_done_removes_merged_and_leaves_unmerged(db: Path, tmp_path: Path) -> None:
    root = _repo(tmp_path)
    merged = _session_for_worktree(root, "merged")
    unmerged = _session_for_worktree(root, "unmerged")
    (unmerged / "change").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(unmerged), "add", "change"], check=True)
    subprocess.run(["git", "-C", str(unmerged), "commit", "-qm", "change"], check=True)
    result = runner.invoke(
        cli.app, ["worktree", "prune-done", "--repo", str(root), "--into", "HEAD"]
    )
    assert result.exit_code == 0
    assert not merged.exists()
    assert unmerged.exists()

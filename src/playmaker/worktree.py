"""Git worktree creation and inspection helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Worktree:
    root: Path
    path: Path
    branch: str


def repo_root(cwd: Path) -> Path:
    result = _git(cwd, "rev-parse", "--show-toplevel")
    if result.returncode:
        raise WorktreeError(f"{cwd} is not a git work tree")
    return Path(result.stdout.strip()).resolve()


def prepare(cwd: Path, name: str, base: str | None, log_path: Path, timeout: float) -> Worktree:
    root = repo_root(cwd)
    path = root.parent / f"{root.name}-wt" / name
    branch = f"wp/{name}"
    base = base or _git(root, "rev-parse", "HEAD").stdout.strip()
    hook = next(
        (
            p
            for p in (root / ".playmaker/wt-setup.sh", root / "scripts/playmaker/wt-setup.sh")
            if p.is_file() and p.stat().st_mode & 0o111
        ),
        None,
    )
    if hook:
        with log_path.open("ab") as log:
            try:
                result = subprocess.run(
                    [str(hook), str(path), branch, base],
                    cwd=root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise WorktreeError("worktree setup hook timed out", 124) from exc
        if result.returncode:
            raise WorktreeError("worktree setup hook failed", result.returncode)
        return Worktree(root, path, branch)
    registered = {entry["path"] for entry in list_worktrees(root)}
    if str(path) in registered:
        return Worktree(root, path, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        result = _git(root, "worktree", "add", str(path), branch)
    else:
        result = _git(root, "worktree", "add", "-b", branch, str(path), base)
    if result.returncode:
        raise WorktreeError(result.stderr.strip() or "git worktree add failed", result.returncode)
    return Worktree(root, path, branch)


def list_worktrees(root: Path) -> list[dict[str, str]]:
    result = _git(root, "worktree", "list", "--porcelain")
    if result.returncode:
        raise WorktreeError(result.stderr.strip() or "cannot list worktrees")
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
        else:
            key, _, value = line.partition(" ")
            if key == "worktree":
                key = "path"
            elif key == "branch":
                value = value.removeprefix("refs/heads/")
            current[key] = value
    return records


def dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain").stdout.strip())


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], text=True, capture_output=True, check=False
    )

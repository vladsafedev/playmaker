from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "playmaker-coach"
    / "scripts"
    / "review-board.sh"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


@pytest.fixture
def auto_repo(tmp_path: Path) -> tuple[Path, str, str, str, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")

    (repo / "a.py").write_text("base\n", encoding="utf-8")
    (repo / "reg.py").write_text("base\n", encoding="utf-8")
    git(repo, "add", "a.py", "reg.py")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")

    (repo / "a.py").write_text("feature\n", encoding="utf-8")
    (repo / "reg.py").write_text("registered\n", encoding="utf-8")
    git(repo, "commit", "-qam", "feature")

    (repo / "a.py").write_text("wip\n", encoding="utf-8")
    git(repo, "commit", "-qam", "wip: wp r1 (auto)")
    wip = git(repo, "rev-parse", "HEAD")

    git(repo, "checkout", "-q", "--detach", base)
    (repo / "other.txt").write_text("divergent\n", encoding="utf-8")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-qm", "divergent")
    divergent = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach", wip)

    review_dir = repo / ".playmaker" / "reviews" / "wp"
    review_dir.mkdir(parents=True)
    (review_dir / "spec.md").write_text(
        "## SCOPE\n- a.py\n- reg.py\n\n## ACCEPTANCE\n1. Feature works.\n",
        encoding="utf-8",
    )
    (repo / ".playmaker" / "reviewers.conf").write_text(
        "precheck fake - precheck\nnormal fake - correctness\n",
        encoding="utf-8",
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "playmaker"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"id\":\"x\",\"agent\":\"impl\",\"status\":\"done\"}'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return repo, base, wip, divergent, env


def run_auto(
    repo: Path, wip: str, env: dict[str, str], *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--auto",
            "wp",
            wip,
            "--impl-id",
            "x",
            "--paths",
            "a.py",
            "--dry-run",
            *extra,
        ],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_dry_run_reuses_recorded_package_base_and_scope_paths(auto_repo) -> None:
    repo, base, wip, _divergent, env = auto_repo
    env_file = repo / ".playmaker" / "reviews" / "wp" / "board.env"
    env_file.write_text(f"required=2\npackage_base={base}\n", encoding="utf-8")

    result = run_auto(repo, wip, env)

    assert result.returncode == 0, result.stdout
    assert f"precheck base: {base} (source: board.env)" in result.stdout
    assert "precheck paths: a.py reg.py" in result.stdout


def test_dry_run_uses_run_base_without_persisting_it(auto_repo) -> None:
    repo, _base, wip, _divergent, env = auto_repo
    env_file = repo / ".playmaker" / "reviews" / "wp" / "board.env"

    result = run_auto(repo, wip, env)

    assert result.returncode == 0, result.stdout
    assert f"precheck base: {wip} (source: this run)" in result.stdout
    assert "would write package_base=" in result.stdout
    assert not env_file.exists()


def test_dry_run_reports_stale_package_base_reset(auto_repo) -> None:
    repo, _base, wip, divergent, env = auto_repo
    env_file = repo / ".playmaker" / "reviews" / "wp" / "board.env"
    env_file.write_text(f"package_base={divergent}\n", encoding="utf-8")

    result = run_auto(repo, wip, env)

    assert result.returncode == 0, result.stdout
    assert "not an ancestor of run base; reset" in result.stdout
    assert f"precheck base: {wip} (source: this run)" in result.stdout
    assert env_file.read_text(encoding="utf-8") == f"package_base={divergent}\n"


def test_package_base_option_wins_and_rejects_non_ancestor(auto_repo) -> None:
    repo, base, wip, divergent, env = auto_repo
    env_file = repo / ".playmaker" / "reviews" / "wp" / "board.env"
    env_file.write_text(f"package_base={wip}\n", encoding="utf-8")

    result = run_auto(repo, wip, env, "--package-base", base)
    rejected = run_auto(repo, wip, env, "--package-base", divergent)

    assert result.returncode == 0, result.stdout
    assert f"precheck base: {base} (source: --package-base)" in result.stdout
    assert rejected.returncode != 0
    assert "must be an ancestor of run base" in rejected.stdout

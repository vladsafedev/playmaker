"""Two seats sharing one lane+lens pair must not overwrite each other's files."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

BOARD = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "playmaker-coach"
    / "scripts"
    / "review-board.sh"
)

# Same stub shape as test_review_board_effort: every dispatch gets a
# lane-derived session id, every session reports done with a verdict.
STUB = """#!/usr/bin/env bash
LOG="${PLAYMAKER_LOG:?}"
joined="$*"
printf '%s\\n' "${joined//$'\\n'/ }" >> "$LOG"
if [[ "$1" == "dispatch" ]]; then
  printf '{"session_id":"sess-%s"}' "$2"
elif [[ "$1" == "get" ]]; then
  printf '%s' '{"status":"done","output":"{\\"verdict\\":\\"pass\\",\
\\"findings\\":[],\\"reviewer\\":\\"lane\\",\\"lens\\":\\"correctness\\"}"}'
elif [[ "$1" == "list" ]]; then
  printf '%s' '[]'
else
  printf '%s' '{}'
fi
exit 0
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")

    (repo / "a.py").write_text("base\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("feature\n", encoding="utf-8")

    review_dir = repo / ".playmaker" / "reviews" / "wp"
    review_dir.mkdir(parents=True)
    (review_dir / "spec.md").write_text(
        "class: feature\n\n## SCOPE\n- a.py\n\n## ACCEPTANCE\n1. Feature works.\n"
        "\n## DONE when\nFeature works.\n",
        encoding="utf-8",
    )
    (repo / ".playmaker" / "reviewers.conf").write_text(
        "normal codex gpt-5.6-terra correctness\n"
        "normal codex gpt-5.6-sol correctness\n",
        encoding="utf-8",
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "playmaker"
    fake.write_text(STUB, encoding="utf-8")
    fake.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["PLAYMAKER_LOG"] = str(tmp_path / "argv.log")
    env["PLAYMAKER_DEAD"] = ""
    Path(env["PLAYMAKER_LOG"]).write_text("", encoding="utf-8")
    return repo, base, env


def run(repo: Path, env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", *argv],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_repeated_lane_and_lens_keep_distinct_files(tmp_path: Path) -> None:
    repo, base, env = make_repo(tmp_path)

    dispatched = run(repo, env, str(BOARD), "wp", base, "--risk", "normal", "--no-precheck")

    assert dispatched.returncode == 0, dispatched.stdout
    review_dir = repo / ".playmaker" / "reviews" / "wp"
    first = review_dir / "prompt-codex-correctness.md"
    second = review_dir / "prompt-codex-correctness-2.md"
    assert first.is_file()
    assert second.is_file()
    assert '"reviewer" to "codex/gpt-5.6-terra"' in first.read_text(encoding="utf-8")
    assert '"reviewer" to "codex/gpt-5.6-sol"' in second.read_text(encoding="utf-8")
    sessions = review_dir / "sessions.txt"
    assert sessions.read_text(encoding="utf-8").splitlines() == [
        "sess-codex|codex|gpt-5.6-terra|correctness",
        "sess-codex|codex|gpt-5.6-sol|correctness",
    ]

    collected = run(repo, env, str(BOARD), "--collect", "wp")

    assert collected.returncode == 0, collected.stdout
    assert "verdicts: 2/2" in collected.stdout
    for name in ("verdict-codex-correctness.json", "verdict-codex-correctness-2.json"):
        verdict = json.loads((review_dir / name).read_text(encoding="utf-8"))
        assert verdict["verdict"] == "pass"

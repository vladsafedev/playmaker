from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

BOARD = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "playmaker-coach"
    / "scripts"
    / "review-board.sh"
)
DECISION = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "playmaker-coach"
    / "scripts"
    / "review-decision.sh"
)

# A stub playmaker: records every argv as one flattened line in $PLAYMAKER_LOG,
# answers precheck --sync dispatches with a passing verdict, gives every
# dispatch a lane-derived session id, and reports the sessions named in
# $PLAYMAKER_DEAD (space-separated ids) as failed. The canned session output
# satisfies both the board verdict contract and the decision answer contract.
STUB = """#!/usr/bin/env bash
LOG="${PLAYMAKER_LOG:?}"
joined="$*"
printf '%s\\n' "${joined//$'\\n'/ }" >> "$LOG"
if [[ "$1" == "dispatch" && "$*" == *--sync* ]]; then
  case " ${PLAYMAKER_PRECHECK_UNAVAILABLE:-} " in
    *" $2 "*) printf '%s' '{}'; exit 0 ;;
  esac
  printf '%s' '{"verdict":"pass","covered":[{"item":"Feature works.",\
"evidence":"a.py:1"}],"missing":[]}'
elif [[ "$1" == "dispatch" ]]; then
  printf '{"session_id":"sess-%s"}' "$2"
elif [[ "$1" == "get" ]]; then
  sid="$2"
  case " $PLAYMAKER_DEAD " in
    *" $sid "*) printf '%s' '{"status":"failed","fail_reason":"boom"}' ;;
    *) printf '%s' '{"status":"done","output":"{\\"verdict\\":\\"pass\\",\
\\"findings\\":[],\\"recommendation\\":\\"A\\",\\"confidence\\":\\"high\\",\
\\"strongest_counter\\":\\"B has merit.\\",\\"breaks\\":[],\
\\"would_change_my_mind\\":\\"Evidence against A.\\"}"}' ;;
  esac
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


@pytest.fixture
def effort_repo(tmp_path: Path) -> tuple[Path, str, dict[str, str], Path]:
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
    (repo / "question.md").write_text(
        "# Should we ship it?\n- A) Ship it.\n- B) Hold it.\nQuestion: A or B?\n",
        encoding="utf-8",
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "playmaker"
    fake.write_text(STUB, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "argv.log"
    log.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["PLAYMAKER_LOG"] = str(log)
    env["PLAYMAKER_DEAD"] = ""
    return repo, base, env, log


def write_roster(repo: Path, text: str) -> Path:
    conf = repo / ".playmaker" / "reviewers.conf"
    conf.write_text(text, encoding="utf-8")
    return conf


def run(
    repo: Path, env: dict[str, str], *argv: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", *argv],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def dispatches(log: Path) -> list[str]:
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("dispatch ")
    ]


def for_lane(lines: list[str], lane: str, sync: bool | None = None) -> list[str]:
    out = [line for line in lines if line.split(" ")[1] == lane]
    if sync is not None:
        out = [line for line in out if ("--sync" in line) == sync]
    return out


def roster_whitespace(text: str, style: str) -> str:
    if style == "tabs":
        return text.replace(" ", "\t")
    if style == "indented":
        return "".join(f" \t  {line}" for line in text.splitlines(keepends=True))
    return text


@pytest.mark.parametrize("style", ["spaces", "tabs", "indented"])
def test_auto_board_effort_end_to_end(effort_repo, style: str) -> None:
    repo, base, env, log = effort_repo
    write_roster(
        repo,
        roster_whitespace(
            "precheck codex pc-model precheck high\n"
            "high codex gpt-6-astra risk ultra\n"
            "high agy m-agy correctness\n"
            "high kimi m-kimi contracts -\n"
            "high claude opus risk xhigh\n",
            style,
        ),
    )
    env["PLAYMAKER_DEAD"] = "sess-codex"  # the ultra seat dies; the reserve subs in

    result = run(repo, env, str(BOARD), "--auto", "wp", base, "--impl-id", "x",
                 "--risk", "high", "--paths", "a.py")

    assert result.returncode == 0, result.stdout
    assert "GREEN" in result.stdout
    lines = dispatches(log)
    (codex,) = for_lane(lines, "codex", sync=False)
    assert "--effort ultra" in codex
    (agy,) = for_lane(lines, "agy", sync=False)
    assert "--effort" not in agy  # four-column line: argv identical to before
    (kimi,) = for_lane(lines, "kimi", sync=False)
    assert "--effort" not in kimi  # '-' means none
    (sub,) = for_lane(lines, "claude", sync=False)
    assert "--effort xhigh" in sub  # the substitute carries ITS OWN line's effort
    (pre,) = for_lane(lines, "codex", sync=True)
    assert "--effort high" in pre
    assert "reserve (not seated): claude · opus · risk · effort xhigh" in result.stdout
    assert "precheck → codex/pc-model · effort high" in result.stdout
    sessions = repo / ".playmaker" / "reviews" / "wp" / "sessions.txt"
    assert sessions.read_text(encoding="utf-8").splitlines() == [
        "sess-codex|codex|gpt-6-astra|risk",
        "sess-agy|agy|m-agy|correctness",
        "sess-kimi|kimi|m-kimi|contracts",
        "sess-claude|claude|opus|risk",  # sessions.txt keeps its 4-field shape
    ]


def test_board_comment_is_neither_effort_nor_lens(effort_repo) -> None:
    repo, base, env, log = effort_repo
    write_roster(
        repo,
        "high codex gpt-6-astra risk ultra # owner note here\n"
        "high agy m-agy correctness\n"
        "high kimi m-kimi contracts\n",
    )

    dry = run(repo, env, str(BOARD), "wp", base, "--risk", "high",
              "--no-precheck", "--dry-run")
    assert dry.returncode == 0, dry.stdout
    assert "would dispatch: codex (gpt-6-astra) / risk · effort ultra" in dry.stdout
    assert "# owner" not in dry.stdout

    result = run(repo, env, str(BOARD), "wp", base, "--risk", "high", "--no-precheck")
    assert result.returncode == 0, result.stdout
    (codex,) = for_lane(dispatches(log), "codex", sync=False)
    assert "--effort ultra" in codex
    sessions = repo / ".playmaker" / "reviews" / "wp" / "sessions.txt"
    assert sessions.read_text(encoding="utf-8").splitlines()[0] == \
        "sess-codex|codex|gpt-6-astra|risk"
    prompt = repo / ".playmaker" / "reviews" / "wp" / "prompt-codex-risk.md"
    assert "YOUR LENS: risk" in prompt.read_text(encoding="utf-8")


def test_board_invalid_effort_names_the_line(effort_repo) -> None:
    repo, base, env, _log = effort_repo
    conf = write_roster(
        repo,
        "high codex gpt-6-astra risk ultra\n"
        "high codex gpt-5.6-sol correctness turbo\n"
        "high kimi m-kimi contracts\n",
    )

    result = run(repo, env, str(BOARD), "wp", base, "--risk", "high",
                 "--no-precheck", "--dry-run")

    assert result.returncode != 0
    assert "turbo" in result.stdout
    assert conf.name in result.stdout
    assert "line 2" in result.stdout


def test_board_claude_ultra_names_the_line(effort_repo) -> None:
    repo, base, env, _log = effort_repo
    conf = write_roster(
        repo,
        "high codex gpt-6-astra risk ultra\n"
        "high claude-api opus correctness ultra\n"
        "high kimi m-kimi contracts\n",
    )

    result = run(repo, env, str(BOARD), "wp", base, "--risk", "high",
                 "--no-precheck", "--dry-run")

    assert result.returncode != 0
    assert "ultra" in result.stdout
    assert "claude-api" in result.stdout
    assert conf.name in result.stdout
    assert "line 2" in result.stdout


def test_board_effort_on_other_lane_is_dropped_with_a_note(effort_repo) -> None:
    repo, base, env, log = effort_repo
    conf = write_roster(
        repo,
        "high codex gpt-6-astra risk ultra\n"
        "high agy m-agy correctness high\n"
        "high kimi m-kimi contracts\n",
    )

    result = run(repo, env, str(BOARD), "wp", base, "--risk", "high", "--no-precheck")

    assert result.returncode == 0, result.stdout
    assert f"{conf} line 2" in result.stdout
    assert "lane agy does not take --effort; ignored" in result.stdout
    lines = dispatches(log)
    (codex,) = for_lane(lines, "codex", sync=False)
    assert "--effort ultra" in codex
    (agy,) = for_lane(lines, "agy", sync=False)
    assert "--effort" not in agy


def test_auto_dry_run_invalid_effort_names_the_line(effort_repo) -> None:
    repo, base, env, _log = effort_repo
    conf = write_roster(
        repo,
        "precheck codex pc-model precheck\n"
        "high codex gpt-6-astra risk turbo\n"
        "high agy m-agy correctness\n"
        "high kimi m-kimi contracts\n",
    )

    result = run(repo, env, str(BOARD), "--auto", "wp", base, "--impl-id", "x",
                 "--risk", "high", "--paths", "a.py", "--dry-run")

    assert result.returncode != 0
    assert "turbo" in result.stdout
    assert conf.name in result.stdout
    assert "line 2" in result.stdout


def test_auto_dry_run_invalid_precheck_effort_names_the_line(effort_repo) -> None:
    repo, base, env, _log = effort_repo

    def dry_run() -> subprocess.CompletedProcess[str]:
        return run(repo, env, str(BOARD), "--auto", "wp", base, "--impl-id", "x",
                   "--risk", "high", "--paths", "a.py", "--dry-run")

    conf = write_roster(
        repo,
        "precheck codex pc-model precheck turbo\n"
        "high codex gpt-6-astra risk ultra\n"
        "high agy m-agy correctness\n"
        "high kimi m-kimi contracts\n",
    )
    result = dry_run()
    assert result.returncode != 0
    assert "turbo" in result.stdout
    assert conf.name in result.stdout
    assert "line 1" in result.stdout

    # a bad token on a later fallback line must stop the dry run too, even
    # though the preview only shows the valid first line
    write_roster(
        repo,
        "precheck codex pc-model precheck high\n"
        "precheck codex pc-model2 precheck turbo\n"
        "high codex gpt-6-astra risk ultra\n"
        "high agy m-agy correctness\n"
        "high kimi m-kimi contracts\n",
    )
    result = dry_run()
    assert result.returncode != 0
    assert "turbo" in result.stdout
    assert conf.name in result.stdout
    assert "line 2" in result.stdout


DECISION_ROSTER = (
    "decision codex gpt-6-astra risk ultra\n"
    "decision manus standard product # owner order 2026-11-03\n"
    "decision agy m-agy correctness -\n"
    "decision claude opus architecture xhigh # coordinator bucket\n"
)


def test_decision_dry_run_shows_effort_and_clean_lens(effort_repo) -> None:
    repo, _base, env, _log = effort_repo
    write_roster(repo, DECISION_ROSTER)

    result = run(repo, env, str(DECISION), "slug", "--question", "question.md",
                 "--dry-run")

    assert result.returncode == 0, result.stdout
    assert "codex · risk · gpt-6-astra · effort ultra" in result.stdout
    seat_lines = [line for line in result.stdout.splitlines()
                  if line.startswith("  ") and " · " in line]
    (manus,) = [line for line in seat_lines if line.split(" · ")[0].strip() == "manus"]
    assert manus == "  manus · product · standard"
    (agy,) = [line for line in seat_lines if line.split(" · ")[0].strip() == "agy"]
    assert "effort" not in agy
    assert "# owner" not in result.stdout
    prompt = repo / ".playmaker" / "decision-reviews" / "slug" / "prompt-manus.md"
    assert "Your lens: product." in prompt.read_text(encoding="utf-8")


@pytest.mark.parametrize("style", ["spaces", "tabs", "indented"])
def test_decision_dispatch_passes_effort(effort_repo, style: str) -> None:
    repo, _base, env, log = effort_repo
    write_roster(repo, roster_whitespace(DECISION_ROSTER, style))

    result = run(repo, env, str(DECISION), "slug", "--question", "question.md")

    assert result.returncode == 0, result.stdout
    assert "Agreement" in result.stdout
    lines = dispatches(log)
    (codex,) = for_lane(lines, "codex")
    assert "--effort ultra" in codex
    (manus,) = for_lane(lines, "manus")
    assert "--effort" not in manus  # a trailing comment is not an effort
    (agy,) = for_lane(lines, "agy")
    assert "--effort" not in agy  # '-' means none
    assert for_lane(lines, "claude") == []  # no --opus, no fourth seat
    seats = repo / ".playmaker" / "decision-reviews" / "slug" / "seats.txt"
    assert seats.read_text(encoding="utf-8").splitlines() == [
        "codex|gpt-6-astra|risk",
        "manus|standard|product",
        "agy|m-agy|correctness",
    ]


def test_decision_opus_seat_carries_its_effort(effort_repo) -> None:
    repo, _base, env, log = effort_repo
    write_roster(repo, DECISION_ROSTER)

    result = run(repo, env, str(DECISION), "slug", "--question", "question.md",
                 "--opus")

    assert result.returncode == 0, result.stdout
    (claude,) = for_lane(dispatches(log), "claude")
    assert "--effort xhigh" in claude
    seats = repo / ".playmaker" / "decision-reviews" / "slug" / "seats.txt"
    assert "claude|opus|architecture" in seats.read_text(encoding="utf-8").splitlines()


def test_decision_invalid_effort_names_the_line(effort_repo) -> None:
    repo, _base, env, _log = effort_repo
    conf = write_roster(
        repo,
        "decision codex gpt-6-astra risk ultra\n"
        "decision codex gpt-5.6-sol product turbo\n"
        "decision agy m-agy correctness\n",
    )

    result = run(repo, env, str(DECISION), "slug", "--question", "question.md",
                 "--dry-run")

    assert result.returncode != 0
    assert "turbo" in result.stdout
    assert conf.name in result.stdout
    assert "line 2" in result.stdout


def test_decision_effort_on_other_lane_is_dropped_with_a_note(effort_repo) -> None:
    repo, _base, env, log = effort_repo
    conf = write_roster(
        repo,
        "decision codex gpt-6-astra risk ultra\n"
        "decision manus standard product high\n"
        "decision agy m-agy correctness\n",
    )

    result = run(repo, env, str(DECISION), "slug", "--question", "question.md")

    assert result.returncode == 0, result.stdout
    assert f"{conf} line 2" in result.stdout
    assert "lane manus does not take --effort; ignored" in result.stdout
    lines = dispatches(log)
    (codex,) = for_lane(lines, "codex")
    assert "--effort ultra" in codex
    (manus,) = for_lane(lines, "manus")
    assert "--effort" not in manus


def test_decision_dispatch_stderr_kept_out_of_session_id_parse(effort_repo) -> None:
    repo, _base, env, _log = effort_repo
    write_roster(repo, DECISION_ROSTER)
    fake = Path(env["PATH"].split(os.pathsep)[0]) / "playmaker"
    noisy = STUB.replace(
        'elif [[ "$1" == "dispatch" ]]; then\n  printf \'{"session_id":"sess-%s"}\' "$2"',
        'elif [[ "$1" == "dispatch" ]]; then\n'
        '  echo "lane warning: stderr must not join the dispatch file" >&2\n'
        '  printf \'{"session_id":"sess-%s"}\' "$2"',
    )
    assert noisy != STUB
    fake.write_text(noisy, encoding="utf-8")

    result = run(repo, env, str(DECISION), "slug", "--question", "question.md")

    assert result.returncode == 0, result.stdout
    assert "Agreement" in result.stdout
    out_dir = repo / ".playmaker" / "decision-reviews" / "slug"
    for lane in ("codex", "manus", "agy"):
        dispatch_file = out_dir / f"dispatch-{lane}.txt"
        assert json.loads(dispatch_file.read_text(encoding="utf-8"))["session_id"] == (
            f"sess-{lane}"
        )
        stderr_file = out_dir / f"dispatch-{lane}.stderr"
        assert "stderr must not join the dispatch file" in stderr_file.read_text(
            encoding="utf-8"
        )


def run_mode(
    repo: Path, base: str, env: dict[str, str], mode: str
) -> subprocess.CompletedProcess[str]:
    if mode.startswith("decision"):
        args = [str(DECISION), "slug", "--question", "question.md"]
    else:
        args = [str(BOARD)]
        if mode.startswith("auto"):
            args.append("--auto")
        args += ["wp", base, "--risk", "high", "--impl-agent", "ignored"]
        if mode.startswith("auto"):
            args += ["--impl-id", "x", "--paths", "a.py"]
        elif mode.startswith("precheck-only"):
            args.append("--precheck-only")
        elif mode == "board-no-precheck":
            args.append("--no-precheck")
    if mode.endswith("-dry"):
        args.append("--dry-run")
    return run(repo, env, *args)


@pytest.mark.parametrize(
    "mode",
    ["auto", "auto-dry", "board", "board-dry", "board-no-precheck",
     "precheck-only", "precheck-only-dry", "decision", "decision-dry"],
)
@pytest.mark.parametrize(
    ("index", "bad_row", "bad_token"),
    [
        (1, "precheck\tcodex\tpc-model\tprecheck\tturbo", "turbo"),
        (2, " \t  precheck claude pc-model precheck turbo", "turbo"),
        (3, "high codex gpt-6-astra risk ultra stray", "stray"),
        (6, "high codex m-op risk turbo", "turbo"),
        (10, "decision claude opus architecture turbo", "turbo"),
        (11, "normal codex m-other correctness turbo", "turbo"),
        (11, "high codex m-impl risk turbo", "turbo"),
        (11, "future-kind other m-other risk - stray", "stray"),
    ],
    ids=["second-precheck-tabs", "third-precheck-indented", "extra-field",
         "reserve", "opus-not-selected", "other-risk", "implementer", "other-kind"],
)
def test_whole_roster_rejected_before_any_use(
    effort_repo, mode: str, index: int, bad_row: str, bad_token: str
) -> None:
    repo, base, env, log = effort_repo
    rows = [
        "precheck stub pc-model precheck high",
        "precheck second pc-model precheck medium",
        "precheck third pc-model precheck low",
        "high codex gpt-6-astra risk ultra",
        "high agy m-agy correctness",
        "high kimi m-kimi contracts -",
        "high opencode m-op risk max",
        *DECISION_ROSTER.splitlines(),
        "normal other m-other correctness",
    ]
    rows[index] = bad_row
    # Physical line numbers include comments and blanks. No final newline is required.
    conf = write_roster(repo, "# roster\n\n" + "\n".join(rows))

    result = run_mode(repo, base, env, mode)

    assert result.returncode != 0, result.stdout
    assert bad_token in result.stdout
    assert f"{conf} line {index + 3}" in result.stdout
    assert log.read_text(encoding="utf-8") == ""  # even auto's first get is too early
    assert "  seat:" not in result.stdout
    assert "precheck lane:" not in result.stdout
    assert "would dispatch:" not in result.stdout
    assert "Decision review" not in result.stdout
    assert not (repo / ".playmaker" / "reviews" / "wp" / "board.env").exists()
    assert not (repo / ".playmaker" / "decision-reviews").exists()


@pytest.mark.parametrize("style", ["tabs", "indented"])
def test_auto_preview_uses_whitespace_and_comment_rules(effort_repo, style: str) -> None:
    repo, base, env, log = effort_repo
    write_roster(repo, roster_whitespace(
        "# header\n\n"
        "precheck codex pc-model precheck medium #ignored turbo stray\n"
        "high codex gpt-6-astra risk ultra #ignored turbo stray\n"
        "high agy m-agy correctness #ignored turbo stray\n"
        "high kimi m-kimi contracts - #ignored turbo stray\n"
        "high claude opus contracts max #ignored turbo stray",
        style,
    ))

    result = run_mode(repo, base, env, "auto-dry")

    assert result.returncode == 0, result.stdout
    assert "  seat: codex gpt-6-astra / risk · effort ultra" in result.stdout
    assert "  seat: agy m-agy / correctness\n" in result.stdout
    assert "  seat: kimi m-kimi / contracts\n" in result.stdout
    assert "  reserve: claude opus / contracts · effort max" in result.stdout
    assert "  precheck lane: precheck codex pc-model precheck · effort medium" in result.stdout
    assert "ignored" not in result.stdout
    assert dispatches(log) == []


@pytest.mark.parametrize("effort", ["", "-", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_precheck_effort_argv(effort_repo, effort: str) -> None:
    repo, base, env, log = effort_repo
    write_roster(repo, f" \tprecheck\tcodex\tpc-model\tprecheck\t{effort}\t#comment turbo stray")

    result = run_mode(repo, base, env, "precheck-only")

    assert result.returncode == 0, result.stdout
    prompt = repo / ".playmaker" / "reviews" / "wp" / "precheck-prompt.md"
    expected = (f"dispatch codex --sync --read-only --batch precheck-wp-r1 --cwd {repo} "
                f"--prompt-file {prompt} --model pc-model")
    if effort not in ("", "-"):
        expected += f" --effort {effort}"
    assert dispatches(log) == [expected]


def test_precheck_fallbacks_use_parsed_rows(effort_repo) -> None:
    repo, base, env, log = effort_repo
    write_roster(repo,
                 "precheck agy pc-model precheck #comment\n"
                 "precheck\tclaude\tpc-model\tprecheck\tmedium\n"
                 " \t precheck codex pc-model precheck ultra #comment")
    env["PLAYMAKER_PRECHECK_UNAVAILABLE"] = "agy claude"

    result = run_mode(repo, base, env, "precheck-only")

    assert result.returncode == 0, result.stdout
    lines = dispatches(log)
    assert [line.split()[1] for line in lines] == ["agy", "claude", "codex"]
    assert "--effort" not in lines[0]
    assert "--effort medium" in lines[1]
    assert "--effort ultra" in lines[2]
    board_env = repo / ".playmaker" / "reviews" / "wp" / "board.env"
    assert "precheck_lane=codex/pc-model" in board_env.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["board", "decision"])
def test_comment_before_lens_cannot_become_a_seat(effort_repo, mode: str) -> None:
    repo, base, env, log = effort_repo
    write_roster(repo,
                 "high bogus model #comment risk turbo\n"
                 "decision bogus model #comment product turbo\n"
                 "high codex gpt-6-astra risk #comment turbo stray\n"
                 "high agy m-agy correctness #comment turbo stray\n"
                 "high kimi m-kimi contracts #comment turbo stray\n"
                 + DECISION_ROSTER)

    result = run_mode(repo, base, env, mode)

    assert result.returncode == 0, result.stdout
    assert for_lane(dispatches(log), "bogus") == []
    assert "#comment" not in result.stdout


@pytest.mark.parametrize("risk", ["routine", "normal", "high", "seams", "decision"])
def test_copy_of_live_four_column_roster(effort_repo, risk: str) -> None:
    repo, base, env, log = effort_repo
    live = Path.home() / ".playmaker" / "reviewers.conf"
    if not live.is_file():
        pytest.skip("no live reviewers.conf on this machine")
    # Only the temporary copy is passed to the scripts; HOME and PATH stay isolated.
    conf = write_roster(repo, live.read_text(encoding="utf-8"))
    rows = []
    for line in conf.read_text(encoding="utf-8").splitlines():
        fields = []
        for field in line.split():
            if field.startswith("#"):
                break
            fields.append(field)
        if fields:
            assert len(fields) == 4, "this compatibility fixture must be a four-column roster"
            rows.append(fields)
    eligible = [row[1:] for row in rows if row[0] == risk]
    assert eligible

    if risk == "decision":
        expected = [row for row in eligible if row[0] != "claude"][:3]
        expected += [row for row in eligible if row[0] == "claude"][:1]
        result = run(repo, env, str(DECISION), "slug", "--question", "question.md", "--opus")
        seats_file = repo / ".playmaker" / "decision-reviews" / "slug" / "seats.txt"
    else:
        required = {"routine": 1, "normal": 2, "high": 3, "seams": 1}[risk]
        expected = eligible[:required]
        preview = run(repo, env, str(BOARD), "--auto", "wp", base, "--impl-id", "x",
                      "--risk", risk, "--paths", "a.py", "--dry-run")
        assert preview.returncode == 0, preview.stdout
        preview_lines = [line for line in preview.stdout.splitlines()
                         if line.startswith(("  seat:", "  reserve:"))]
        assert preview_lines == [
            f"  {'seat' if i < required else 'reserve'}: {lane} {model} / {lens}"
            for i, (lane, model, lens) in enumerate(eligible)
        ]
        result = run(repo, env, str(BOARD), "wp", base, "--risk", risk, "--no-precheck")
        for lane, model, lens in eligible[required:]:
            assert f"reserve (not seated): {lane} {model} {lens}" in result.stdout
        seats_file = repo / ".playmaker" / "reviews" / "wp" / "sessions.txt"

    assert result.returncode == 0, result.stdout
    commands = dispatches(log)
    assert len(commands) == len(expected)
    assert all("--effort" not in command for command in commands)
    for lane, model, _lens in expected:
        assert any(command.startswith(f"dispatch {lane} ")
                   and (f"--model {model}" in command if model != "-"
                        else "--model" not in command)
                   for command in commands)
    expected_records = ["|".join(row) for row in expected]
    records = seats_file.read_text(encoding="utf-8").splitlines()
    if risk != "decision":
        expected_records = [f"sess-{row[0]}|{record}"
                            for row, record in zip(expected, expected_records, strict=True)]
    assert records == expected_records

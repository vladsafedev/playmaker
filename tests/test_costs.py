import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from playmaker import costs
from playmaker.cli import app


@pytest.fixture(autouse=True)
def isolated_costs_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "playmaker"
    home.mkdir()
    monkeypatch.setattr(costs.state, "PLAYMAKER_HOME", home)
    for name, relative in (
        ("DB_PATH", "state.db"),
        ("QUOTAS_PATH", "quotas.json"),
        ("LOGS_DIR", "logs"),
    ):
        monkeypatch.setattr(costs.state, name, home / relative)
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    monkeypatch.setattr(costs, "LAUNCH_AGENT_PATH", tmp_path / "Library" / "agent.plist")

    def forbidden(*args, **kwargs):
        pytest.fail("costs tests must not call providers or launchctl")

    from playmaker import quotas

    monkeypatch.setattr(quotas, "refresh_all", forbidden)
    monkeypatch.setattr(costs, "run_launchctl", forbidden)


def _database(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (agent, model, cwd, worktree_path, repo_root, started_at, "
        "finished_at, duration_seconds, cost_usd, agent_session_id, session_file_path)"
    )
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_project_matching_includes_worktrees_and_repo_root(tmp_path: Path) -> None:
    root = tmp_path / "team"
    assert costs.session_matches_project({"repo_root": str(root)}, root)
    assert costs.session_matches_project({"cwd": f"{root}-wt/fix"}, root)
    assert costs.session_matches_project({"worktree_path": str(root / ".claude/worktrees/x")}, root)
    assert not costs.session_matches_project({"cwd": str(tmp_path / "other")}, root)


def test_ambiguous_project_name_is_an_error() -> None:
    rows = [{"repo_root": "/one/team"}, {"repo_root": "/two/team"}]
    with pytest.raises(costs.CostsError, match="ambiguous"):
        costs.resolve_project("team", rows)


def test_report_sums_metered_and_prorates_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, other = tmp_path / "team", tmp_path / "other"
    db = tmp_path / "state.db"
    _database(
        db,
        [
            (
                "codex",
                None,
                str(root),
                None,
                str(root),
                "2026-06-01T12:00:00Z",
                None,
                10,
                3,
                None,
                None,
            ),
            (
                "codex",
                None,
                str(other),
                None,
                str(other),
                "2026-06-01T12:00:00Z",
                None,
                30,
                0,
                None,
                None,
            ),
        ],
    )
    plans = tmp_path / "plans.toml"
    plans.write_text(
        '[[plan]]\nname="Pro"\ngroup="codex"\nlanes=["codex"]\nmonthly_usd=30\nstart="2026-06-01"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    report = costs.build_report(
        "team", date(2026, 6, 1), date(2026, 7, 1), db_path=db, subscriptions_path=plans
    )
    assert report["totals"]["metered_usd"] == 0
    assert report["totals"]["subscriptions_usd"] == 7.5


def test_claude_transcript_deduplicates_and_excludes_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "team"
    directory = tmp_path / "claude" / "project"
    directory.mkdir(parents=True)
    record = {
        "timestamp": "2026-06-01T12:00:00Z",
        "sessionId": "code",
        "cwd": str(root),
        "message": {
            "id": "one",
            "model": "claude-opus-5-5[1m]",
            "usage": {
                "input_tokens": 1000000,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1000000,
                    "ephemeral_1h_input_tokens": 1000000,
                },
            },
        },
    }
    excluded = json.loads(json.dumps(record))
    excluded["sessionId"] = "api"
    (directory / "a.jsonl").write_text(
        "\n".join(map(json.dumps, [record, record, excluded])), encoding="utf-8"
    )
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    usage, _ = costs.claude_usage(
        root,
        [{"agent": "claude-api", "agent_session_id": "api"}],
        date(2026, 6, 1),
        date(2026, 6, 2),
        [],
    )
    assert usage["models"]["claude-opus-5-5"]["input_tokens"] == 1000000
    assert usage["api_equivalent_usd"] == 17


def test_snapshot_deduplicates_cached_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "playmaker"
    home.mkdir(exist_ok=True)
    quotas = home / "quotas.json"
    quotas.write_text('{"fetched_at":"2099-01-01T00:00:00+00:00","providers":{}}')
    monkeypatch.setattr(costs.state, "QUOTAS_PATH", quotas)
    monkeypatch.setattr(costs.state, "PLAYMAKER_HOME", home)
    assert costs.snapshot()
    assert not costs.snapshot()


def test_manus_credits_are_read_and_converted_from_task_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, home = tmp_path / "team", tmp_path / "playmaker"
    db = tmp_path / "state.db"
    _database(
        db,
        [
            (
                "manus",
                None,
                str(root),
                None,
                str(root),
                "2026-06-01T12:00:00Z",
                None,
                None,
                None,
                "task-1",
                None,
            )
        ],
    )
    (home / "manus").mkdir(parents=True)
    (home / "manus" / "task-1.jsonl").write_text(
        '{"type":"playmaker_meta","credit_usage":4}\n', encoding="utf-8"
    )
    plans = tmp_path / "plans.toml"
    plans.write_text(
        '[[plan]]\nname="Manus"\ngroup="manus"\nlanes=["manus"]\n'
        'usd_per_credit=2.5\nstart="2026-06-01"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(costs.state, "PLAYMAKER_HOME", home)
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    report = costs.build_report(
        "team", date(2026, 6, 1), date(2026, 6, 2), db_path=db, subscriptions_path=plans
    )
    assert report["metered"]["lanes"]["manus_credits"] == 4
    assert report["metered"]["lanes"]["manus"] == 10
    assert report["totals"]["metered_usd"] == 10


def test_subscription_prorates_month_boundary_and_plan_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, other, db = tmp_path / "team", tmp_path / "other", tmp_path / "state.db"
    rows = []
    for day in ("2026-05-20", "2026-06-20", "2026-07-01"):
        for project in (root, other):
            rows.append(
                (
                    "codex",
                    None,
                    str(project),
                    None,
                    str(project),
                    f"{day}T12:00:00Z",
                    None,
                    10,
                    None,
                    None,
                    None,
                )
            )
    _database(db, rows)
    plans = tmp_path / "plans.toml"
    plans.write_text(
        '[[plan]]\nname="Old"\ngroup="codex"\nlanes=["codex"]\nmonthly_usd=31\n'
        'start="2026-05-20"\nend="2026-06-15"\n\n'
        '[[plan]]\nname="New"\ngroup="codex"\nlanes=["codex"]\nmonthly_usd=62\n'
        'start="2026-06-15"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    report = costs.build_report(
        "team", date(2026, 5, 20), date(2026, 7, 2), db_path=db, subscriptions_path=plans
    )
    # Old: 12/31 May; New: 16/30 June + 1/31 July, each at 50% share.
    assert report["totals"]["subscriptions_usd"] == pytest.approx(23.533333333333335)


def test_subscription_usage_uses_count_when_no_duration_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, other, db = tmp_path / "team", tmp_path / "other", tmp_path / "state.db"
    rows = [
        (
            "codex",
            None,
            str(root),
            None,
            str(root),
            "2026-06-01T12:00:00Z",
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "codex",
            None,
            str(other),
            None,
            str(other),
            "2026-06-01T12:00:00Z",
            None,
            None,
            None,
            None,
            None,
        ),
    ]
    _database(db, rows)
    plans = tmp_path / "plans.toml"
    plans.write_text(
        '[[plan]]\nname="Pro"\ngroup="codex"\nlanes=["codex"]\nmonthly_usd=30\nstart="2026-06-01"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    report = costs.build_report(
        "team", date(2026, 6, 1), date(2026, 7, 1), db_path=db, subscriptions_path=plans
    )
    assert report["totals"]["subscriptions_usd"] == 15


def test_pending_and_price_less_plans_are_listed_but_not_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = tmp_path / "team", tmp_path / "state.db"
    _database(
        db,
        [
            (
                "codex",
                None,
                str(root),
                None,
                str(root),
                "2026-06-01T12:00:00Z",
                None,
                10,
                None,
                None,
                None,
            )
        ],
    )
    plans = tmp_path / "plans.toml"
    plans.write_text(
        '[[plan]]\nname="Pending"\ngroup="one"\nlanes=["codex"]\nmonthly_usd=30\n'
        'start="2026-06-01"\nstatus="pending"\n\n'
        '[[plan]]\nname="Unknown price"\ngroup="two"\nlanes=["codex"]\nstart="2026-06-01"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    report = costs.build_report(
        "team", date(2026, 6, 1), date(2026, 7, 1), db_path=db, subscriptions_path=plans
    )
    assert report["totals"]["subscriptions_usd"] == 0
    assert {item["state"] for item in report["subscriptions"]["plans"]} == {
        "pending",
        "price missing",
    }


def test_unknown_claude_model_keeps_tokens_without_a_guessed_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, directory = tmp_path / "team", tmp_path / "claude" / "project"
    directory.mkdir(parents=True)
    record = {
        "timestamp": "2026-06-01T12:00:00Z",
        "sessionId": "code",
        "cwd": str(root),
        "message": {
            "id": "unknown",
            "model": "claude-future-9",
            "usage": {"input_tokens": 9, "output_tokens": 2, "cache_read_input_tokens": 1},
        },
    }
    (directory / "a.jsonl").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    usage, all_usage = costs.claude_usage(root, [], date(2026, 6, 1), date(2026, 6, 2), [])
    assert usage["models"]["claude-future-9"]["input_tokens"] == 9
    assert usage["models"]["claude-future-9"]["price"] is None
    assert usage["api_equivalent_usd"] == all_usage["all"] == 0


def test_agent_plist_and_injected_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path, logs = tmp_path / "agent.plist", tmp_path / "logs"
    monkeypatch.setattr(costs, "LAUNCH_AGENT_PATH", plist_path)
    monkeypatch.setattr(costs.state, "LOGS_DIR", logs)
    monkeypatch.setattr(costs.shutil, "which", lambda _: "/opt/bin/playmaker")
    document = costs.plistlib.loads(costs.agent_plist(42))
    assert document["ProgramArguments"] == ["/opt/bin/playmaker", "costs", "snapshot"]
    assert document["StartInterval"] == 42
    assert document["RunAtLoad"] is True
    calls: list[list[str]] = []
    monkeypatch.setattr(costs, "run_launchctl", lambda args: calls.append(args))
    costs.install_agent(interval=42, print_only=False)
    assert calls == [["bootstrap", f"gui/{costs.os.getuid()}", str(plist_path)]]
    assert costs.plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]["PATH"]


def test_costs_json_output_has_report_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = tmp_path / "team", tmp_path / "state.db"
    _database(
        db,
        [
            (
                "codex",
                None,
                str(root),
                None,
                str(root),
                "2026-06-01T12:00:00Z",
                None,
                1,
                2,
                None,
                None,
            )
        ],
    )
    monkeypatch.setattr(costs.state, "DB_PATH", db)
    monkeypatch.setattr(costs, "CLAUDE_PROJECTS_PATH", tmp_path / "claude")
    result = CliRunner().invoke(app, ["costs", "--project", "team", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert {"metered", "subscriptions", "claude_code", "totals", "inputs", "warnings"} <= set(
        report
    )


def test_malformed_task_lines_are_skipped_and_bad_plan_is_rejected(tmp_path: Path) -> None:
    task = tmp_path / "task.jsonl"
    task.write_text('not json\n{"type":"playmaker_meta","credit_usage":3}\n', encoding="utf-8")
    warnings: list[str] = []
    records = costs._read_json_lines(task, warnings)
    assert records[0]["credit_usage"] == 3
    assert warnings == [f"skipped 1 malformed line(s) in {task}"]
    plans = tmp_path / "bad.toml"
    plans.write_text(
        '[[plan]]\nname="x"\ngroup="x"\nlanes=[]\nstart="not-a-date"\n', encoding="utf-8"
    )
    with pytest.raises(costs.CostsError, match="invalid start date"):
        costs.load_plans(plans)


def _session(
    root: Path,
    agent: str = "codex",
    day: str = "2026-06-01",
    duration: float | None = 10,
    cost: float | None = None,
    task: str | None = None,
    model: str | None = None,
    finish: str | None = None,
    file_path: Path | None = None,
) -> tuple:
    return (
        agent,
        model,
        str(root),
        None,
        str(root),
        f"{day}T12:00:00Z",
        finish,
        duration,
        cost,
        task,
        str(file_path) if file_path else None,
    )


def _plans(content: str) -> Path:
    path = costs.costs_dir() / "subscriptions.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _record(root: Path, identifier: str = "one", day: str = "2026-06-01", **message_fields) -> dict:
    return {
        "timestamp": f"{day}T12:00:00Z",
        "sessionId": "code",
        "cwd": str(root),
        "message": {
            "id": identifier,
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 1_000_000},
            **message_fields,
        },
    }


def _transcript(name: str, records: list[dict], extra: str = "") -> Path:
    path = costs.CLAUDE_PROJECTS_PATH / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records) + extra)
    return path


def _report(root: Path, since: date | None = date(2026, 6, 1), until=date(2026, 7, 1)):
    return costs.build_report(str(root), since, until)


def _assert_totals_agree(report: dict) -> None:
    total = report["totals"]
    assert sum(total["lanes"].values()) == pytest.approx(total["spent_usd"])
    assert sum(sum(day.values()) for day in total["by_day"].values()) == pytest.approx(
        total["spent_usd"]
    )


# 1: Price each task once, using its billing date and only an active plan.
def test_manus_pricing_respects_dates_status_group_and_task_deduplication(tmp_path: Path) -> None:
    root = tmp_path / "team"
    _database(
        costs.state.DB_PATH,
        [
            _session(root, "manus", "2026-06-01", task="one"),
            _session(root, "manus", "2026-06-15", task="two"),
            _session(root, "manus", "2026-06-16", task="two"),
            _session(root, "manus", "2026-06-30", task="unpriced"),
        ],
    )
    task_dir = costs.state.PLAYMAKER_HOME / "manus"
    task_dir.mkdir()
    for name in ("one", "two", "unpriced"):
        (task_dir / f"{name}.jsonl").write_text('{"type":"playmaker_meta","credit_usage":4}\n')
    _plans(
        '[[plan]]\nname="Ended"\ngroup="manus"\nlanes=["manus"]\n'
        'start="2026-05-01"\nend="2026-06-01"\nusd_per_credit=100\n'
        '[[plan]]\nname="Old"\ngroup="manus"\nlanes=["manus"]\n'
        'start="2026-06-01"\nend="2026-06-15"\nusd_per_credit=2\n'
        '[[plan]]\nname="New"\ngroup="manus"\nlanes=["manus"]\n'
        'start="2026-06-15"\nend="2026-06-30"\nusd_per_credit=3\n'
        '[[plan]]\nname="Pending"\ngroup="manus"\nlanes=["manus"]\n'
        'start="2026-06-01"\nstatus="pending"\nusd_per_credit=1000\n'
        '[[plan]]\nname="Future"\ngroup="manus"\nlanes=["manus"]\n'
        'start="2026-07-01"\nusd_per_credit=10000\n'
    )
    report = _report(root)
    assert report["metered"]["lanes"] == {"manus_credits": 12, "manus": 20}
    assert report["totals"]["by_day"] == {"2026-06-01": {"manus": 8}, "2026-06-15": {"manus": 12}}
    _assert_totals_agree(report)


def test_manus_does_not_sum_rates_from_conflicting_groups(tmp_path: Path) -> None:
    root = tmp_path / "team"
    _database(costs.state.DB_PATH, [_session(root, "manus", task="one")])
    task_dir = costs.state.PLAYMAKER_HOME / "manus"
    task_dir.mkdir()
    (task_dir / "one.jsonl").write_text('{"type":"playmaker_meta","credit_usage":4}\n')
    _plans(
        "".join(
            f'[[plan]]\nname="{group}"\ngroup="{group}"\nlanes=["manus"]\n'
            'start="2026-06-01"\nusd_per_credit=2\n'
            for group in ("one", "two")
        )
    )
    with pytest.raises(costs.CostsError, match="ambiguous Manus credit plans"):
        _report(root)


# 2: Both explicit/default --since modes retain per-file warnings, including cache hits.
@pytest.mark.parametrize("since", [None, date(2026, 6, 1)])
def test_transcript_malformed_counts_survive_period_selection_and_cache(
    tmp_path: Path, since: date | None
) -> None:
    root = tmp_path / "team"
    invalid_usage = _record(root, "bad", usage={"input_tokens": "bad"})
    path = _transcript(
        "one.jsonl",
        [_record(root), invalid_usage],
        extra=('not json\n{"broken":}\n{"message":{"usage":\n[]\n'),
    )
    other_path = _transcript("two.jsonl", [], extra='{"message":{"usage":[]}}\n')
    for _ in range(2):
        report = _report(root, since)
        assert report["warnings"] == [
            f"skipped 5 malformed line(s) in {path}",
            f"skipped 1 malformed line(s) in {other_path}",
        ]
        assert report["claude_code"]["api_equivalent_usd"] == 4
        assert report["period"]["since"] == "2026-06-01"


# 3: Only actual per-call USD lanes contribute; subscription transcripts remain informational.
def test_subscription_lane_costs_are_never_metered(tmp_path: Path) -> None:
    root = tmp_path / "team"
    _database(
        costs.state.DB_PATH,
        [
            _session(root, "claude", cost=7.74),
            _session(root, "codex", cost=100),
            _session(root, "unknown", cost=200),
            _session(root, "manus", cost=500),
            _session(root, "claude-api", cost=2, task="api"),
            _session(root, "perplexity", cost=3),
        ],
    )
    api_record = _record(root, "api-message")
    api_record["sessionId"] = "api"
    _transcript("project.jsonl", [_record(root), api_record])
    _plans(
        '[[plan]]\nname="Max"\ngroup="claude"\nlanes=["claude","claude-code"]\n'
        'start="2026-06-01"\nmonthly_usd=30\n'
    )
    report = _report(root)
    assert report["metered"]["lanes"] == {"claude-api": 2, "perplexity": 3}
    assert report["totals"]["metered_usd"] == 5
    assert report["totals"]["subscriptions_usd"] == 30
    assert report["totals"]["spent_usd"] == 35
    assert report["claude_code"]["api_equivalent_usd"] == 4
    _assert_totals_agree(report)


# 4: repo_root wins over cwd; worktrees never become project-name candidates.
def test_project_name_prefers_repo_roots_and_filters_worktrees(tmp_path: Path) -> None:
    root = tmp_path / "source" / "team"
    rows = [
        {"repo_root": str(root)},
        {"repo_root": str(tmp_path / "apollo"), "cwd": str(tmp_path / "apollo-wt" / "team")},
        {"repo_root": str(tmp_path / "stray-wt" / "team")},
        {"cwd": str(tmp_path / "elsewhere" / "team")},
        {"repo_root": str(tmp_path / "repo" / ".claude" / "worktrees" / "team")},
    ]
    assert costs.resolve_project("team", rows) == root
    assert costs.resolve_project(str(tmp_path / "apollo-wt/team"), rows) == (
        tmp_path / "apollo-wt/team"
    )
    fallback = tmp_path / "fallback" / "team"
    assert (
        costs.resolve_project(
            "team", [{"cwd": str(fallback)}, {"cwd": str(tmp_path / "apollo-wt/team")}]
        )
        == fallback
    )
    with pytest.raises(costs.CostsError, match="candidates: none"):
        costs.resolve_project("team", [rows[1], rows[2], rows[4]])


@pytest.mark.parametrize("field", ["repo_root", "cwd"])
def test_project_ambiguity_lists_only_eligible_candidates(tmp_path: Path, field: str) -> None:
    one, two = tmp_path / "one/team", tmp_path / "two/team"
    with pytest.raises(costs.CostsError) as error:
        costs.resolve_project(
            "team", [{field: str(one)}, {field: str(two)}, {field: str(tmp_path / "other-wt/team")}]
        )
    assert "ambiguous" in str(error.value)
    assert str(one) in str(error.value) and str(two) in str(error.value)
    assert "other-wt" not in str(error.value)


def test_transcript_worktree_matching_uses_cwd_not_directory_spellings(tmp_path: Path) -> None:
    root = tmp_path / "team"
    assert costs.session_matches_project({"cwd": str(root / "src")}, root)
    assert not costs.session_matches_project({"cwd": f"{root}-wt-fix"}, root)
    assert not costs.session_matches_project({"cwd": f"{root}--claude-worktrees-fix"}, root)
    for index, cwd in enumerate((Path(f"{root}-wt/fix"), root / ".claude/worktrees/fix")):
        _transcript(f"arbitrary-directory/{index}.jsonl", [_record(cwd, str(index))])
    assert _report(root)["claude_code"]["api_equivalent_usd"] == 8


# 5: Change PLAYMAKER_HOME after import; all costs paths follow each call.
def test_costs_paths_follow_call_time_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "team"
    _transcript("one.jsonl", [_record(root)])
    costs.state.QUOTAS_PATH.write_text('{"fetched_at":"2099-01-01T00:00:00Z","providers":{}}')
    for number in (1, 2):
        home = tmp_path / f"home-{number}"
        monkeypatch.setattr(costs.state, "PLAYMAKER_HOME", home)
        _plans(
            f'[[plan]]\nname="Plan {number}"\ngroup="max"\nlanes=["claude-code"]\n'
            f'start="2026-06-01"\nmonthly_usd={number * 30}\n'
        )
        assert costs.load_plans()[0]["name"] == f"Plan {number}"
        listed = CliRunner().invoke(app, ["costs", "plans"])
        assert listed.exit_code == 0 and f"Plan {number}:" in listed.output
        assert _report(root)["totals"]["subscriptions_usd"] == number * 30
        assert (home / "costs/claude-usage-cache.json").exists()
        assert costs.snapshot()
        assert not costs.snapshot()
        assert (home / "costs/quota-snapshots.jsonl").exists()


# 6: The latest numeric metadata wins even after nulls or malformed values.
@pytest.mark.parametrize("latest", [0, 4])
def test_manus_uses_last_numeric_credit_usage(tmp_path: Path, latest: int) -> None:
    root = tmp_path / "team"
    task_path = tmp_path / "legacy-task.jsonl"
    task_path.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                {"type": "playmaker_meta", "credit_usage": 8},
                {"type": "playmaker_meta", "credit_usage": latest},
                {"type": "playmaker_meta", "credit_usage": None},
                {"type": "playmaker_meta", "credit_usage": "invalid"},
                {"type": "playmaker_meta", "credit_usage": True},
            ]
        )
    )
    _database(
        costs.state.DB_PATH, [_session(root, "manus", task="missing-primary", file_path=task_path)]
    )
    _plans(
        '[[plan]]\nname="Manus"\ngroup="manus"\nlanes=["manus"]\n'
        'start="2026-06-01"\nusd_per_credit=2\n'
    )
    report = _report(root)
    assert report["metered"]["lanes"]["manus_credits"] == latest
    assert report["metered"]["lanes"]["manus"] == latest * 2
    _assert_totals_agree(report)


# 7: Split the actual plan charge by this project's lane usage, never repeat it.
@pytest.mark.parametrize("use_durations", [True, False])
def test_multilane_subscription_totals_split_by_project_usage(
    tmp_path: Path, use_durations: bool
) -> None:
    root, other = tmp_path / "team", tmp_path / "other"
    if use_durations:
        rows = [
            _session(root, "codex", duration=10),
            _session(root, "claude", duration=None, finish="2026-06-01T12:00:30Z"),
            _session(other, "codex", duration=80),
        ]
    else:
        rows = (
            [_session(root, "codex", duration=None)]
            + [_session(root, "claude", duration=None) for _ in range(3)]
            + [_session(other, "codex", duration=None) for _ in range(8)]
        )
    _database(costs.state.DB_PATH, rows)
    _plans(
        '[[plan]]\nname="Bundle"\ngroup="bundle"\nlanes=["codex","claude"]\n'
        'start="2026-06-01"\nmonthly_usd=120\n'
    )
    report = _report(root)
    assert report["totals"]["subscriptions_usd"] == 40
    assert report["totals"]["lanes"] == {"codex": 10, "claude": 30}
    _assert_totals_agree(report)


def test_multilane_split_without_usage_is_equal() -> None:
    assert costs._split_lane_charge(30, ["codex", "claude"], [], 0) == {"codex": 15, "claude": 15}


# 8: Report the unattributed subscription amount and the Claude Code share in text.
def test_text_report_prints_unattributed_and_claude_share(tmp_path: Path) -> None:
    root, other = tmp_path / "team", tmp_path / "other"
    _transcript("one.jsonl", [_record(root), _record(other, "two")])
    _plans(
        '[[plan]]\nname="Idle"\ngroup="idle"\nlanes=["codex"]\nstart="2026-06-01"\nmonthly_usd=30\n'
    )
    result = CliRunner().invoke(
        app, ["costs", "--project", str(root), "--since", "2026-06-01", "--until", "2026-07-01"]
    )
    assert result.exit_code == 0, result.output
    assert "Unattributed — Idle: $30.00" in result.output.split("Claude Code")[0]
    assert "Project share: 50.00%" in result.output.split("Claude Code")[1].split("Totals")[0]


def test_usage_in_other_projects_is_not_unattributed(tmp_path: Path) -> None:
    root, other = tmp_path / "team", tmp_path / "other"
    _database(costs.state.DB_PATH, [_session(other)])
    _plans(
        '[[plan]]\nname="Used"\ngroup="codex"\nlanes=["codex"]\n'
        'start="2026-06-01"\nmonthly_usd=30\n'
    )
    report = _report(root)
    assert report["subscriptions"]["unattributed_usd"] == {}
    assert report["totals"]["spent_usd"] == 0


# 9: Each month/plan overlap is spread evenly, with sums matching lane/overall totals.
def test_subscription_by_day_spans_month_boundary_and_plan_change(tmp_path: Path) -> None:
    root = tmp_path / "team"
    _database(
        costs.state.DB_PATH,
        [_session(root, day=day) for day in ("2026-05-30", "2026-06-01", "2026-06-02")],
    )
    _plans(
        '[[plan]]\nname="Old"\ngroup="codex"\nlanes=["codex"]\nmonthly_usd=31\n'
        'start="2026-05-30"\nend="2026-06-02"\n'
        '[[plan]]\nname="New"\ngroup="codex"\nlanes=["codex"]\nmonthly_usd=60\n'
        'start="2026-06-02"\nend="2026-06-04"\n'
    )
    report = _report(root, date(2026, 5, 29), date(2026, 6, 5))
    by_day = report["totals"]["by_day"]
    assert set(by_day) == {"2026-05-30", "2026-05-31", "2026-06-01", "2026-06-02", "2026-06-03"}
    assert by_day["2026-05-30"] == by_day["2026-05-31"] == {"subscription:Old": 1}
    assert by_day["2026-06-01"]["subscription:Old"] == pytest.approx(31 / 30)
    assert by_day["2026-06-02"] == by_day["2026-06-03"] == {"subscription:New": 2}
    _assert_totals_agree(report)


# 10: Non-object caches/snapshot lines are skipped and surfaced, never AttributeError.
@pytest.mark.parametrize("payload", [[], None, "text", 42])
def test_snapshot_skips_nonobject_quota_cache_with_warning(payload: object) -> None:
    costs.state.QUOTAS_PATH.write_text(json.dumps(payload))
    result = CliRunner().invoke(app, ["costs", "snapshot"])
    assert result.exit_code == 0, result.output
    assert "Warning: skipped malformed quota cache" in result.output
    assert not (costs.costs_dir() / "quota-snapshots.jsonl").exists()


@pytest.mark.parametrize("bad_line", [[], None, "text", {"quotas": []}, {"quotas": None}])
def test_snapshot_skips_nonobject_lines_and_dedupes_last_valid_payload(bad_line: object) -> None:
    payload = {"fetched_at": "2099-01-01T00:00:00Z", "providers": {}}
    costs.state.QUOTAS_PATH.write_text(json.dumps(payload))
    path = costs.costs_dir() / "quota-snapshots.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bad_line) + "\n")
    warnings = []
    assert costs.snapshot(warnings)
    assert warnings and "skipped 1 malformed" in warnings[0]
    with path.open("a") as stream:
        stream.write(json.dumps(bad_line) + "\n")
    before = path.read_bytes()
    warnings = []
    assert not costs.snapshot(warnings)
    assert warnings and path.read_bytes() == before


def test_snapshot_refreshes_stale_cache_only_via_injected_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playmaker import quotas

    costs.state.QUOTAS_PATH.write_text('{"fetched_at":"2020-01-01T00:00:00Z","providers":{}}')
    calls = []

    def refresh(path):
        calls.append(path)
        path.write_text('{"fetched_at":"2099-01-01T00:00:00Z","providers":{}}')

    monkeypatch.setattr(quotas, "refresh_all", refresh)
    assert costs.snapshot()
    assert not costs.snapshot()
    assert calls == [costs.state.QUOTAS_PATH]


# 11: Local placeholders affect neither models, share, dedupe nor default since.
def test_synthetic_claude_records_are_dropped(tmp_path: Path) -> None:
    root = tmp_path / "team"
    _transcript(
        "one.jsonl",
        [
            _record(root, "one", day="2026-05-01", model="<synthetic>"),
            _record(root, "one"),
            _record(tmp_path / "other", "other", model="<synthetic>"),
        ],
    )
    for _ in range(2):
        report = _report(root, since=None)
        assert report["period"]["since"] == "2026-06-01"
        assert set(report["claude_code"]["models"]) == {"claude-opus-5-5"}
        assert report["claude_code"]["share"] == 1
        assert report["claude_code"]["api_equivalent_usd"] == 4
        assert report["warnings"] == []


# 12: Pending upgrades may overlap; active entries of the same group may not.
def test_plan_overlap_ignores_pending_but_rejects_active() -> None:
    content = (
        '[[plan]]\nname="Current"\ngroup="codex"\nlanes=["codex"]\n'
        'start="2026-06-01"\nmonthly_usd=30\n'
        '[[plan]]\nname="Upgrade"\ngroup="codex"\nlanes=["codex"]\n'
        'start="2026-06-15"\nmonthly_usd=60\nstatus="pending"\n'
    )
    path = _plans(content)
    assert len(costs.load_plans()) == 2
    result = CliRunner().invoke(app, ["costs", "plans"])
    assert result.exit_code == 0 and "Upgrade: pending" in result.output
    path.write_text(content.replace('status="pending"', 'status="active"'))
    with pytest.raises(costs.CostsError, match="overlapping dates"):
        costs.load_plans()
    result = CliRunner().invoke(app, ["costs", "plans"])
    assert result.exit_code == 2 and "overlapping dates" in result.output


# 13: Overrides are exclusively top-level and reprice cached raw usage.
def test_top_level_prices_override_without_plans_and_reprice_cached_usage(tmp_path: Path) -> None:
    root = tmp_path / "team"
    _transcript(
        "one.jsonl",
        [
            _record(
                root,
                model="claude-opus-5-5[1m]",
                usage={
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "cache_read_input_tokens": 1_000_000,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 1_000_000,
                        "ephemeral_1h_input_tokens": 1_000_000,
                    },
                },
            )
        ],
    )
    content = '[api_prices."claude-opus-5-5"]\ninput=2\noutput=3\ncache_read=0.5\n'
    path = _plans(content)
    assert costs.load_plans() == []
    assert _report(root)["claude_code"]["api_equivalent_usd"] == 12
    path.write_text(content.replace("input=2", "input=4"))
    assert _report(root)["claude_code"]["api_equivalent_usd"] == 20.5
    # An absolute top-level table after [[plan]] is still accepted by TOML.
    path.write_text(
        '[[plan]]\nname="Max"\ngroup="max"\nlanes=["claude-code"]\n'
        'start="2026-06-01"\nmonthly_usd=30\n' + content
    )
    assert _report(root)["claude_code"]["api_equivalent_usd"] == 12


@pytest.mark.parametrize(
    "prices",
    [
        '[plan.api_prices."claude-opus-5-5"]\ninput=1\noutput=2\ncache_read=3\n',
        '[api_prices."claude-opus-5-5"]\ninput=1\noutput=2\n',
        '[api_prices."claude-opus-5-5"]\ninput="bad"\noutput=2\ncache_read=3\n',
        '[api_prices."claude-opus-5-5"]\ninput=1\noutput=2\ncache_read=3\nunknown=4\n',
    ],
)
def test_invalid_or_per_plan_price_overrides_are_rejected(prices: str) -> None:
    _plans(
        '[[plan]]\nname="Max"\ngroup="max"\nlanes=["claude-code"]\nstart="2026-06-01"\n' + prices
    )
    result = CliRunner().invoke(app, ["costs", "plans"])
    assert result.exit_code == 2, result.output
    assert "api" in result.output


# 14: Count source reader calls, including default-since discovery, cache invalidation,
# old-mtime pruning, cross-file dedupe and rebuilding malformed/versioned caches.
@pytest.mark.parametrize("since", [None, date(2026, 6, 1)])
def test_second_report_reads_zero_unchanged_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, since: date | None
) -> None:
    root = tmp_path / "team"
    first = _transcript("one.jsonl", [_record(root)])
    second = _transcript("two.jsonl", [_record(root), _record(root, "two")])
    original = costs._read_claude_file
    calls = []

    def reader(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(costs, "_read_claude_file", reader)
    before = _report(root, since)
    assert calls == [first, second]
    calls.clear()
    after = _report(root, since)
    assert after == before
    assert calls == []
    assert after["claude_code"]["api_equivalent_usd"] == 8
    cache = json.loads((costs.costs_dir() / "claude-usage-cache.json").read_text())
    assert set(cache["files"]) == {str(first.resolve()), str(second.resolve())}
    assert all(
        "message" not in record for item in cache["files"].values() for record in item["records"]
    )


def test_cache_reuses_raw_records_across_projects_periods_and_api_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, other = tmp_path / "team", tmp_path / "other"
    _transcript(
        "one.jsonl",
        [
            _record(root, "early", day="2026-05-01"),
            _record(root),
            _record(other, "two"),
        ],
    )
    assert _report(root)["claude_code"]["api_equivalent_usd"] == 4

    def forbidden(path):
        pytest.fail("unchanged file was parsed again")

    monkeypatch.setattr(costs, "_read_claude_file", forbidden)
    assert _report(other)["claude_code"]["api_equivalent_usd"] == 4
    assert _report(root, date(2026, 5, 1))["claude_code"]["api_equivalent_usd"] == 8
    _database(costs.state.DB_PATH, [_session(root, "claude-api", task="code")])
    assert _report(root)["claude_code"]["api_equivalent_usd"] == 0


@pytest.mark.parametrize("change", ["size", "mtime_ns"])
def test_usage_cache_invalidates_on_size_or_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = tmp_path / "team"
    first = _transcript("one.jsonl", [_record(root)])
    second = _transcript("two.jsonl", [_record(root, "two")])
    _report(root)
    stat = first.stat()
    if change == "size":
        with first.open("a") as stream:
            stream.write(json.dumps(_record(root, "three")) + "\n")
        os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    else:
        first.write_text(first.read_text().replace('"one"', '"new"'))
        os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    calls = []
    original = costs._read_claude_file

    def reader(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(costs, "_read_claude_file", reader)
    report = _report(root)
    assert calls == [first]
    assert second not in calls
    assert report["claude_code"]["api_equivalent_usd"] == (12 if change == "size" else 8)


def test_transcripts_older_than_since_are_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "team"
    old = _transcript("old.jsonl", [_record(root, "old", day="2026-05-01")], extra="broken\n")
    cutoff = datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    os.utime(old, (cutoff - 1, cutoff - 1))
    current = _transcript("current.jsonl", [_record(root)])
    os.utime(current, (cutoff, cutoff))
    original = costs._read_claude_file
    calls = []

    def reader(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(costs, "_read_claude_file", reader)
    report = _report(root)
    assert calls == [current]
    assert report["warnings"] == []
    assert report["claude_code"]["api_equivalent_usd"] == 4
    calls.clear()
    widened = _report(root, since=None)
    assert calls == [old]
    assert widened["period"]["since"] == "2026-05-01"
    assert widened["claude_code"]["api_equivalent_usd"] == 8
    assert widened["warnings"] == [f"skipped 1 malformed line(s) in {old}"]


def test_transcript_reader_only_json_decodes_usage_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "team"
    path = _transcript(
        "one.jsonl",
        [
            {"type": "user", "message": {"content": "a" * 100_000}},
            {"metadata": [{"a": True, "b": None, "c": [-1, 2.3, 'escaped " quote']}], "empty": {}},
            _record(root),
        ],
    )
    original = costs.json.loads
    decoded = []

    def loads(line, *args, **kwargs):
        assert '"usage"' in line
        decoded.append(line)
        return original(line, *args, **kwargs)

    monkeypatch.setattr(costs.json, "loads", loads)
    parsed = costs._read_claude_file(path)
    assert len(decoded) == len(parsed["records"]) == 1
    assert parsed["malformed"] == 0


@pytest.mark.parametrize(
    "broken_cache",
    [
        "not json",
        "[]",
        "null",
        '{"version":-1,"files":{}}',
        '{"version":1,"files":[]}',
    ],
)
def test_corrupt_or_old_usage_cache_is_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_cache: str
) -> None:
    root = tmp_path / "team"
    source = _transcript("one.jsonl", [_record(root)])
    cache = costs.costs_dir() / "claude-usage-cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(broken_cache)
    calls = []
    original = costs._read_claude_file

    def reader(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(costs, "_read_claude_file", reader)
    report = _report(root)
    assert calls == [source]
    assert any("rebuilding Claude usage cache" in item for item in report["warnings"])
    assert report["claude_code"]["api_equivalent_usd"] == 4
    calls.clear()
    assert _report(root)["warnings"] == []
    assert calls == []


def test_malformed_usage_cache_entry_is_rebuilt(tmp_path: Path) -> None:
    root = tmp_path / "team"
    source = _transcript("one.jsonl", [_record(root)])
    _report(root)
    cache = costs.costs_dir() / "claude-usage-cache.json"
    content = json.loads(cache.read_text())
    content["files"][str(source.resolve())]["records"][0]["tokens"] = "invalid"
    cache.write_text(json.dumps(content))
    report = _report(root)
    assert report["claude_code"]["api_equivalent_usd"] == 4
    assert any(
        "rebuilding malformed Claude usage cache entry" in item for item in report["warnings"]
    )

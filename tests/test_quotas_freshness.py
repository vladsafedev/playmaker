from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker import cli  # noqa: E402
from playmaker.cli import app  # noqa: E402

runner = CliRunner()


def _write_snapshot(path: Path, age: timedelta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "fetched_at": (datetime.now(UTC) - age).isoformat(),
                "providers": {"codex": {"status": "ok", "windows": []}},
            }
        ),
        encoding="utf-8",
    )


class _Spy:
    def __init__(self, path: Path) -> None:
        self.calls = 0
        self.path = path

    def __call__(self, target: Path) -> dict:
        self.calls += 1
        _write_snapshot(self.path, timedelta(0))
        return {}


def _patch(monkeypatch, tmp_path: Path, max_age: str = "5m") -> _Spy:
    snapshot = tmp_path / "quotas.json"
    spy = _Spy(snapshot)
    monkeypatch.setattr(cli.state, "QUOTAS_PATH", snapshot)
    monkeypatch.setattr(cli.state, "init_db", lambda: None)
    monkeypatch.setattr(
        cli.config, "setting", lambda section, key, default=None: max_age
    )
    import playmaker.quotas as quotas_mod

    monkeypatch.setattr(quotas_mod, "refresh_all", spy)
    return spy


def test_a_fresh_snapshot_is_printed_without_probing(monkeypatch, tmp_path: Path) -> None:
    spy = _patch(monkeypatch, tmp_path)
    _write_snapshot(spy.path, timedelta(minutes=1))

    result = runner.invoke(app, ["quotas"])

    assert result.exit_code == 0
    assert spy.calls == 0, "a snapshot inside max_age must not trigger probes"


def test_a_stale_snapshot_refreshes_itself(monkeypatch, tmp_path: Path) -> None:
    spy = _patch(monkeypatch, tmp_path)
    _write_snapshot(spy.path, timedelta(hours=4))

    result = runner.invoke(app, ["quotas"])

    assert result.exit_code == 0
    assert spy.calls == 1, "routing off a four-hour-old table is the bug this prevents"


def test_cached_prints_the_stale_snapshot_and_says_so(monkeypatch, tmp_path: Path) -> None:
    spy = _patch(monkeypatch, tmp_path)
    _write_snapshot(spy.path, timedelta(days=4))

    result = runner.invoke(app, ["quotas", "--cached"])

    assert result.exit_code == 0
    assert spy.calls == 0
    assert "stale" in result.output


def test_refresh_probes_even_when_the_snapshot_is_fresh(monkeypatch, tmp_path: Path) -> None:
    spy = _patch(monkeypatch, tmp_path)
    _write_snapshot(spy.path, timedelta(seconds=5))

    result = runner.invoke(app, ["quotas", "--refresh"])

    assert result.exit_code == 0
    assert spy.calls == 1


def test_max_age_is_configurable(monkeypatch, tmp_path: Path) -> None:
    spy = _patch(monkeypatch, tmp_path, max_age="30s")
    _write_snapshot(spy.path, timedelta(minutes=2))

    runner.invoke(app, ["quotas"])

    assert spy.calls == 1, "[quotas] max_age must be honoured"


def test_duration_parser_accepts_the_shapes_config_uses() -> None:
    assert cli._parse_duration("90s", 0) == 90
    assert cli._parse_duration("5m", 0) == 300
    assert cli._parse_duration("1h", 0) == 3600
    assert cli._parse_duration(45, 0) == 45
    assert cli._parse_duration("nonsense", 300) == 300

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas
from playmaker import cli

_WINDOWS = [
    {"name": "Session", "pct_left": 70, "reset_at_iso": "2099-01-01T00:00:00+00:00",
     "reset_relative": "stale text"},
    {"name": "Weekly · Fable", "pct_left": 52, "reset_at_iso": None, "reset_relative": "2d"},
]


def _ok() -> dict:
    return {"status": "ok", "windows": [dict(w) for w in _WINDOWS], "blocks": []}


def _rate_limited() -> dict:
    raise RuntimeError("HTTP 429 https://api.anthropic.com/api/oauth/usage: rate_limit_error")


def test_a_failed_probe_keeps_the_last_good_windows(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "quotas.json"
    monkeypatch.setattr(quotas, "PROBES", {"claude": _ok})
    first = quotas.refresh_all(path)["providers"]["claude"]

    monkeypatch.setattr(quotas, "PROBES", {"claude": _rate_limited})
    failed = quotas.refresh_all(path)["providers"]["claude"]

    # The failure is what the entry says: nothing routes on old numbers.
    assert failed["status"] == "error"
    assert "429" in failed["error"]
    assert "windows" not in failed
    kept = failed["last_good"]
    assert kept["at"] == first["last_success"]
    assert [w["pct_left"] for w in kept["windows"]] == [70, 52]
    # «Resets in» is counted again from now, not copied from the old probe.
    assert kept["windows"][0]["reset_relative"] != "stale text"
    assert kept["windows"][1]["reset_relative"] == "2d"


def test_a_run_of_failures_never_loses_the_numbers(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "quotas.json"
    monkeypatch.setattr(quotas, "PROBES", {"claude": _ok})
    first = quotas.refresh_all(path)["providers"]["claude"]
    monkeypatch.setattr(quotas, "PROBES", {"claude": _rate_limited})
    for _ in range(3):
        quotas.refresh_all(path)

    stored = json.loads(path.read_text())["providers"]["claude"]

    assert stored["last_good"]["at"] == first["last_success"]
    assert stored["last_success"] == first["last_success"]


def test_a_success_replaces_the_kept_numbers(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "quotas.json"
    monkeypatch.setattr(quotas, "PROBES", {"claude": _ok})
    quotas.refresh_all(path)
    monkeypatch.setattr(quotas, "PROBES", {"claude": _rate_limited})
    quotas.refresh_all(path)
    monkeypatch.setattr(quotas, "PROBES", {"claude": _ok})

    again = quotas.refresh_all(path)["providers"]["claude"]

    assert again["status"] == "ok"
    assert "last_good" not in again


def test_a_provider_that_never_had_windows_keeps_nothing(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "quotas.json"
    monkeypatch.setattr(quotas, "PROBES", {"claude": _rate_limited})

    failed = quotas.refresh_all(path)["providers"]["claude"]

    assert "last_good" not in failed


def test_the_table_shows_the_kept_windows_under_the_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "quotas.json"
    monkeypatch.setattr(quotas, "PROBES", {"claude": _ok})
    quotas.refresh_all(path)
    monkeypatch.setattr(quotas, "PROBES", {"claude": _rate_limited})
    failed = quotas.refresh_all(path)["providers"]["claude"]

    with cli.console.capture() as capture:
        cli._render_provider("claude", failed)
    text = capture.get()

    assert "429" in text
    assert "last known, as of" in text
    assert "Weekly · Fable" in text

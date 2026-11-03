from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.config as config
import playmaker.quotas as quotas
from playmaker import cli, state


@pytest.fixture
def configured(monkeypatch):
    def _set(cfg: dict) -> None:
        monkeypatch.setattr(config, "load_config", lambda: cfg)

    return _set


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


def _seed(cost: float) -> None:
    session_id = state.insert_session(agent="perplexity", prompt="p", cwd="/repo")
    with state.connect() as conn:
        conn.execute(
            "UPDATE sessions SET cost_usd = ?, started_at = ? WHERE id = ?",
            (cost, datetime.now(UTC).isoformat(), session_id),
        )
        conn.commit()


def test_perplexity_probe_reports_local_spend_against_its_monthly_cap(db, configured) -> None:
    configured({"agents": {"perplexity": {"monthly_cap_usd": 20}}})
    _seed(0.03)
    _seed(0.17)

    probe = quotas.perplexity_probe()

    assert "perplexity" in quotas.PROBES
    assert probe["status"] == "ok"
    assert "spent $0.20 of $20" in probe["note"]
    assert "$19.80 left" in probe["note"]
    assert "local sum of cost_usd, not the Perplexity balance" in probe["note"]
    assert probe["spent_usd"] == 0.2
    assert probe["left_usd"] == 19.8
    (window,) = probe["windows"]
    assert window["name"] == "Credits"
    assert window["pct_left"] == 99


def test_perplexity_probe_without_sessions_or_cap_has_no_window(db, configured) -> None:
    configured({})

    probe = quotas.perplexity_probe()

    assert probe["status"] == "ok"
    assert probe["note"].startswith("spent $0.00 of an unknown cap since ")
    assert probe["windows"] == []


def test_renderer_shows_the_perplexity_title() -> None:
    with cli.console.capture() as capture:
        cli._render_provider("perplexity", {"status": "ok", "windows": []})

    assert "Perplexity" in capture.get()

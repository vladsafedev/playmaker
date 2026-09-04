"""Codex quota probe coverage for model-scoped Spark limits."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas
from playmaker import cli

# Captured verbatim from chatgpt.com/backend-api/wham/usage on a live Pro Lite
# plan. The account identifiers are intentionally redacted.
_PAYLOAD = {
    "user_id": "<redacted>",
    "account_id": "<redacted>",
    "email": "<redacted>",
    "plan_type": "prolite",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 34,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 218486,
            "reset_at": 1788751180,
        },
        "secondary_window": None,
    },
    "code_review_rate_limit": None,
    "additional_rate_limits": [
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 18000,
                    "reset_at": 1788550695,
                },
                "secondary_window": {
                    "used_percent": 92,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 511704,
                    "reset_at": 1789044398,
                },
            },
            "normal_model_slug": None,
        }
    ],
    "model_usage": {},
    "credits": {
        "has_credits": False,
        "unlimited": False,
        "overage_limit_reached": False,
        "balance": "0",
        "approx_local_messages": [0, 0],
        "approx_cloud_messages": [0, 0],
    },
    "spend_control": {
        "reached": False,
        "individual_limit": None,
    },
    "rate_limit_reached_type": None,
    "promo": None,
    "rate_limit_reset_credits": {
        "available_count": 2,
        "applicable_available_count": 0,
    },
}


def _respond(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(quotas, "_http_json", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        quotas,
        "_codex_load_auth",
        lambda: {"tokens": {"access_token": "tok"}, "account_id": "acct"},
    )


def test_main_and_spark_windows_are_kept_separate(monkeypatch) -> None:
    _respond(monkeypatch, _PAYLOAD)

    result = quotas.codex_probe()

    assert [(window["name"], window["pct_left"]) for window in result["windows"]] == [
        ("Session", 66)
    ]
    assert result["windows"][0]["reserve_pct"] is None
    assert [(window["name"], window["pct_left"]) for window in result["blocks"][0]["windows"]] == [
        ("Session", 100),
        ("Weekly", 8),
    ]
    spark_session, spark_weekly = result["blocks"][0]["windows"]
    assert spark_session["forecast"] is None
    assert spark_weekly["reset_at_iso"].startswith("2026-09-")
    assert spark_weekly["reset_relative"]


def test_missing_or_empty_additional_limits_have_no_spark_block(monkeypatch) -> None:
    for additional_limits in ([], None):
        payload = deepcopy(_PAYLOAD)
        if additional_limits is None:
            del payload["additional_rate_limits"]
        else:
            payload["additional_rate_limits"] = additional_limits
        _respond(monkeypatch, payload)

        assert quotas.codex_probe()["blocks"] == []


def test_non_spark_additional_limit_remains_weekly_reserve(monkeypatch) -> None:
    payload = deepcopy(_PAYLOAD)
    payload["rate_limit"]["secondary_window"] = {
        "used_percent": 25,
        "limit_window_seconds": 604800,
        "reset_after_seconds": 511704,
        "reset_at": 1789044398,
    }
    payload["additional_rate_limits"] = [
        {
            "limit_name": "GPT-5 reserve",
            "metered_feature": "codex_reserve",
            "rate_limit": {
                "secondary_window": {
                    "used_percent": 40,
                    "limit_window_seconds": 604800,
                    "reset_at": 1789044398,
                }
            },
        }
    ]
    _respond(monkeypatch, payload)

    result = quotas.codex_probe()

    assert result["blocks"] == []
    assert result["windows"][1]["name"] == "Weekly"
    assert result["windows"][1]["reserve_pct"] == 60


def test_rendering_draws_spark_as_a_separate_block(monkeypatch) -> None:
    _respond(monkeypatch, _PAYLOAD)
    result = quotas.codex_probe()

    with cli.console.capture() as capture:
        cli._render_provider("codex", result)
    text = capture.get()

    assert text.count("Codex — Spark") == 1
    assert text.index("Codex — Spark") > text.index("Session")

    with cli.console.capture() as capture:
        cli._render_provider(
            "codex",
            {
                "status": "ok",
                "windows": [
                    {
                        "name": "Session",
                        "pct_left": 66,
                        "reset_relative": None,
                        "forecast": None,
                        "reserve_pct": None,
                    }
                ],
            },
        )
    assert "Codex — Spark" not in capture.get()

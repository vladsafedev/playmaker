"""Claude quota probe coverage for model-scoped weekly limits."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas

# Captured verbatim from api.anthropic.com/api/oauth/usage on a live Max plan.
_PAYLOAD = {
    "five_hour": {
        "utilization": 14.0,
        "resets_at": "2026-09-04T19:00:00.208321+00:00",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    },
    "seven_day": {
        "utilization": 46.0,
        "resets_at": "2026-09-05T03:00:00.208343+00:00",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    },
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_cowork": None,
    "seven_day_omelette": None,
    "tangelo": None,
    "iguana_necktie": None,
    "omelette_promotional": None,
    "nimbus_quill": {
        "utilization": 0.0,
        "resets_at": None,
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    },
    "cinder_cove": None,
    "amber_ladder": None,
    "juniper_tide": None,
    "extra_usage": {
        "is_enabled": False,
        "monthly_limit": 2000,
        "used_credits": 0.0,
        "utilization": 0.0,
        "currency": "USD",
        "decimal_places": 2,
        "disabled_reason": "out_of_credits",
        "user_disabled": False,
        "spend_limit_reached": False,
        "credits_ever_enabled": True,
        "daily": None,
        "weekly": None,
    },
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 14,
            "severity": "normal",
            "resets_at": "2026-09-04T19:00:00.208321+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 46,
            "severity": "normal",
            "resets_at": "2026-09-05T03:00:00.208343+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 72,
            "severity": "normal",
            "resets_at": "2026-09-05T03:00:00.208558+00:00",
            "scope": {
                "model": {
                    "id": None,
                    "display_name": "Fable",
                },
                "surface": None,
            },
            "is_active": True,
        },
    ],
    "spend": {
        "used": {
            "amount_minor": 0,
            "currency": "USD",
            "exponent": 2,
        },
        "limit": {
            "amount_minor": 2000,
            "currency": "USD",
            "exponent": 2,
        },
        "percent": 0,
        "severity": "normal",
        "enabled": False,
        "disabled_reason": "out_of_credits",
        "cap": {
            "money": None,
            "credits": {
                "amount_minor": 2000,
                "exponent": 2,
            },
        },
        "balance": None,
        "auto_reload": None,
        "disclaimer": "Usage credits cover you when you hit your plan limits. [Learn more](https://support.claude.com/articles/12429409)",
        "can_purchase_credits": False,
        "can_toggle": False,
    },
    "member_dashboard_available": False,
}


def _respond(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(quotas, "_http_json", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        quotas,
        "_claude_load_keychain",
        lambda: {
            "accessToken": "tok",
            "expiresAt": 4_000_000_000_000,
            "subscriptionType": "max",
        },
    )
    monkeypatch.setattr(quotas, "_claude_email_from_token", lambda token: None)


def test_model_scoped_weekly_row_is_placed_after_weekly(monkeypatch) -> None:
    _respond(monkeypatch, _PAYLOAD)

    result = quotas.claude_probe()

    assert [(window["name"], window["pct_left"]) for window in result["windows"]] == [
        ("Session", 86),
        ("Weekly", 54),
        ("Weekly · Fable", 28),
    ]
    weekly, scoped = result["windows"][1:]
    assert scoped["reset_at_iso"] == _PAYLOAD["limits"][2]["resets_at"]
    assert (scoped["forecast"] is None) == (weekly["forecast"] is None)


def test_inactive_model_scoped_weekly_limit_is_still_rendered(monkeypatch) -> None:
    payload = deepcopy(_PAYLOAD)
    payload["limits"][2]["is_active"] = False
    _respond(monkeypatch, payload)

    assert "Weekly · Fable" in [window["name"] for window in quotas.claude_probe()["windows"]]


@pytest.mark.parametrize("shape", ["missing_name", "null_limits", "absent_limits"])
def test_malformed_or_missing_scoped_limits_are_ignored(monkeypatch, shape: str) -> None:
    payload = deepcopy(_PAYLOAD)
    if shape == "missing_name":
        del payload["limits"][2]["scope"]["model"]["display_name"]
    elif shape == "null_limits":
        payload["limits"] = None
    else:
        del payload["limits"]
    _respond(monkeypatch, payload)

    assert "Weekly · Fable" not in [window["name"] for window in quotas.claude_probe()["windows"]]


def test_scoped_rows_precede_existing_model_window(monkeypatch) -> None:
    payload = deepcopy(_PAYLOAD)
    payload["seven_day_sonnet"] = {
        "utilization": 12.0,
        "resets_at": "2026-09-05T03:00:00.208343+00:00",
    }
    _respond(monkeypatch, payload)

    assert [window["name"] for window in quotas.claude_probe()["windows"]] == [
        "Session",
        "Weekly",
        "Weekly · Fable",
        "Sonnet",
    ]

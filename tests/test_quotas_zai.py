"""Z.ai GLM Coding Plan quota probe."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas

# Captured verbatim from api.z.ai/api/monitor/usage/quota/limit on a live Max
# plan. `percentage` is percent *used*; nextResetTime is absent until a window
# has been touched; TIME_LIMIT is the monthly MCP tool pool, not inference.
_PAYLOAD = {
    "code": 200,
    "msg": "Operation successful",
    "success": True,
    "data": {
        "level": "max",
        "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 0},
            {
                "type": "TOKENS_LIMIT",
                "unit": 6,
                "number": 1,
                "percentage": 5,
                "nextResetTime": 1785339213993,
            },
            {
                "type": "TIME_LIMIT",
                "unit": 5,
                "number": 1,
                "usage": 4000,
                "currentValue": 10,
                "remaining": 3990,
                "percentage": 1,
                "nextResetTime": 1787412813994,
                "usageDetails": [{"modelCode": "search-prime", "usage": 10}],
            },
        ],
    },
}


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    """Point the probe at an opencode auth.json holding a Z.ai credential."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    auth = tmp_path / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"zai-coding-plan": {"type": "api", "key": "sk-test"}}), encoding="utf-8"
    )
    for var in ("ZAI_API_KEY", "Z_AI_API_KEY", "ZHIPUAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return auth


def _respond(monkeypatch, payload: dict) -> dict:
    seen: dict = {}

    def fake(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return payload

    monkeypatch.setattr(quotas, "_http_json", fake)
    return seen


def test_windows_are_labelled_and_percent_left_is_inverted(monkeypatch, keyed) -> None:
    _respond(monkeypatch, _PAYLOAD)

    result = quotas.zai_probe()

    assert result["status"] == "ok"
    assert result["tier"] == "Max"
    assert [(w["name"], w["pct_left"]) for w in result["windows"]] == [
        ("Session", 100),
        ("Weekly", 95),
        ("MCP tools", 99),
    ]


def test_reset_times_are_converted_and_untouched_windows_have_none(monkeypatch, keyed) -> None:
    _respond(monkeypatch, _PAYLOAD)

    session, weekly, _mcp = quotas.zai_probe()["windows"]

    assert session["reset_at_iso"] is None  # never touched, so no reset yet
    assert weekly["reset_at_iso"].startswith("2026-07-29T")
    assert weekly["reset_relative"]


def test_the_session_window_carries_no_forecast(monkeypatch, keyed) -> None:
    # Matches the claude probe: a 5-hour window churns too fast to pace.
    _respond(monkeypatch, _PAYLOAD)

    session = quotas.zai_probe()["windows"][0]

    assert session["forecast"] is None


def test_the_key_is_sent_raw_not_as_a_bearer_token(monkeypatch, keyed) -> None:
    seen = _respond(monkeypatch, _PAYLOAD)

    quotas.zai_probe()

    assert seen["headers"]["Authorization"] == "sk-test"


def test_an_unknown_window_still_renders(monkeypatch, keyed) -> None:
    _respond(
        monkeypatch,
        {"code": 200, "data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 4, "number": 3,
                                           "percentage": 20}]}},
    )

    (window,) = quotas.zai_probe()["windows"]

    assert window["name"] == "3 days"
    assert window["pct_left"] == 80


def test_a_reshaped_payload_is_unsupported_rather_than_an_error(monkeypatch, keyed) -> None:
    _respond(monkeypatch, {"code": 200, "data": {"somethingElse": []}})

    result = quotas.zai_probe()

    assert result["status"] == "unsupported"
    assert "unrecognised" in result["reason"]


def test_an_api_error_is_raised_so_the_aggregator_records_it(monkeypatch, keyed) -> None:
    _respond(monkeypatch, {"code": 401, "success": False, "msg": "invalid api key"})

    with pytest.raises(RuntimeError, match="invalid api key"):
        quotas.zai_probe()


def test_no_credential_is_unsupported_not_an_error(monkeypatch, tmp_path) -> None:
    # An unconfigured provider must not show up as a red error row.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for var in ("ZAI_API_KEY", "Z_AI_API_KEY", "ZHIPUAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    result = quotas.zai_probe()

    assert result["status"] == "unsupported"
    assert "opencode auth login" in result["reason"]


def test_the_environment_is_a_fallback_for_env_injected_configs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("ZAI_API_KEY", "sk-from-env")
    seen = _respond(monkeypatch, _PAYLOAD)

    result = quotas.zai_probe()

    assert result["status"] == "ok"
    assert seen["headers"]["Authorization"] == "sk-from-env"


# The same endpoint on a live Pro plan, 2026-08. z.ai renamed the inference
# windows TOKENS_LIMIT -> CREDIT_LIMIT when plans moved to weekly Credits, and
# stopped returning the monthly TIME_LIMIT pool altogether.
_PAYLOAD_CREDITS = {
    "code": 200,
    "msg": "Operation successful",
    "success": True,
    "data": {
        "level": "pro",
        "limits": [
            {
                "type": "CREDIT_LIMIT",
                "unit": 3,
                "number": 5,
                "usage": 12000,
                "currentValue": 21,
                "remaining": 11978,
                "percentage": 1,
                "nextResetTime": 1787084800394,
            },
            {
                "type": "CREDIT_LIMIT",
                "unit": 6,
                "number": 1,
                "usage": 60000,
                "currentValue": 21,
                "remaining": 59978,
                "percentage": 1,
                "nextResetTime": 1787671183998,
            },
        ],
    },
}


def test_credit_limit_windows_read_like_the_token_ones(monkeypatch, keyed) -> None:
    """The rename must not demote the rows to bare spans ("5 hours"/"1 week").

    The labels are shared with the claude probe on purpose, so the two providers
    line up in the table; an unmapped type still renders but loses that.
    """
    _respond(monkeypatch, _PAYLOAD_CREDITS)

    result = quotas.zai_probe()

    assert result["status"] == "ok"
    assert result["tier"] == "Pro"
    assert [(w["name"], w["pct_left"]) for w in result["windows"]] == [
        ("Session", 99),
        ("Weekly", 99),
    ]

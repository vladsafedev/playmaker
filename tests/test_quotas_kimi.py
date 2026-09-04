"""Kimi Code quota probe."""

from __future__ import annotations

import json
import stat
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas

# Captured verbatim from https://api.kimi.ai/coding/v1/usages on a live
# Moderato (LEVEL_BASIC) account, 2026-09-04. `usage` is the weekly percent
# bucket; each `limits[]` entry is a rolling window, here five hours.
_PAYLOAD = json.loads(
    '{"user":{"userId":"d9gg8obmrb73tddocqb0","region":"REGION_OVERSEA","membership":{"level":"LEVEL_BASIC"},"businessId":""},"usage":{"limit":"100","used":"1","remaining":"99","resetTime":"2026-09-11T14:50:20.391966Z"},"limits":[{"window":{"duration":300,"timeUnit":"TIME_UNIT_MINUTE"},"detail":{"limit":"100","used":"3","remaining":"97","resetTime":"2026-09-04T19:50:20.391966Z"}}],"parallel":{"limit":"10"},"totalQuota":{},"authentication":{"method":"METHOD_ACCESS_TOKEN","scope":"FEATURE_CODING"},"subType":"TYPE_PURCHASE","domain":"DOMAIN_NEXUS","version":"GOODS_VERSION_V1"}'
)


@pytest.fixture
def credential(monkeypatch, tmp_path) -> Path:
    """Point the probe at the Kimi CLI's global managed-provider credential."""
    home = tmp_path / "kimi-code"
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    credentials = home / "credentials" / "kimi-code-env-deadbeef.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        json.dumps(
            {
                "access_token": "access-test",
                "refresh_token": "refresh-test",
                "expires_at": int(time.time()) + 900,
                "expires_in": 900,
                "token_type": "Bearer",
                "scope": "kimi-code",
            }
        ),
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    (home / "config.toml").write_text(
        """[providers.\"managed:kimi-code\"]
base_url = "https://api.kimi.ai/coding/v1"

[providers.\"managed:kimi-code\".oauth]
key = "oauth/kimi-code-env-deadbeef"
oauth_host = "https://auth.kimi.ai"
""",
        encoding="utf-8",
    )
    return credentials


def _respond(monkeypatch, payload: dict) -> dict:
    seen: dict = {}

    def fake(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return payload

    monkeypatch.setattr(quotas, "_http_json", fake)
    return seen


def test_usage_is_normalized_to_session_and_weekly(monkeypatch, credential) -> None:
    _respond(monkeypatch, _PAYLOAD)

    result = quotas.kimi_probe()

    assert result["status"] == "ok"
    assert result["tier"] == "Basic"
    assert [(window["name"], window["pct_left"]) for window in result["windows"]] == [
        ("Session", 97),
        ("Weekly", 99),
    ]


def test_usage_endpoint_and_bearer_token_follow_the_managed_config(monkeypatch, credential) -> None:
    seen = _respond(monkeypatch, _PAYLOAD)

    quotas.kimi_probe()

    assert seen["url"] == "https://api.kimi.ai/coding/v1/usages"
    assert seen["headers"]["Authorization"] == "Bearer access-test"


def test_session_keeps_its_reset_time_but_has_no_pace_forecast(monkeypatch, credential) -> None:
    _respond(monkeypatch, _PAYLOAD)

    session, weekly = quotas.kimi_probe()["windows"]

    assert session["reset_at_iso"] == "2026-09-04T19:50:20.391966Z"
    assert session["forecast"] is None
    assert weekly["reset_at_iso"] == "2026-09-11T14:50:20.391966Z"


def test_an_exhausted_window_without_remaining_still_renders(monkeypatch, credential) -> None:
    payload = json.loads(json.dumps(_PAYLOAD))
    detail = payload["limits"][0]["detail"]
    detail["used"] = "100"
    detail.pop("remaining")
    _respond(monkeypatch, payload)

    session = quotas.kimi_probe()["windows"][0]

    assert session["pct_left"] == 0


def test_expired_token_uses_the_cli_refresh_request_and_persists_it(
    monkeypatch, credential
) -> None:
    expired = json.loads(credential.read_text(encoding="utf-8"))
    expired["expires_at"] = int(time.time()) - 1
    credential.write_text(json.dumps(expired), encoding="utf-8")
    seen: list[tuple[str, dict]] = []

    def fake(url, **kwargs):
        seen.append((url, kwargs))
        if url == "https://auth.kimi.ai/api/oauth/token":
            return {
                "access_token": "access-refreshed",
                "refresh_token": "refresh-refreshed",
                "expires_in": 900,
                "token_type": "Bearer",
                "scope": "kimi-code",
            }
        return _PAYLOAD

    monkeypatch.setattr(quotas, "_http_json", fake)

    result = quotas.kimi_probe()

    assert result["status"] == "ok"
    refresh_url, refresh_kwargs = seen[0]
    assert refresh_url == "https://auth.kimi.ai/api/oauth/token"
    assert refresh_kwargs["method"] == "POST"
    assert refresh_kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(refresh_kwargs["body"]) == {
        "client_id": ["17e5f671-d194-4dfb-9706-5516cb48c098"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-test"],
    }
    saved = json.loads(credential.read_text(encoding="utf-8"))
    assert saved["access_token"] == "access-refreshed"
    assert saved["refresh_token"] == "refresh-refreshed"
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert seen[1][1]["headers"]["Authorization"] == "Bearer access-refreshed"


def test_credential_write_uses_a_mode_600_sibling_temp_file(monkeypatch, credential) -> None:
    seen: dict = {}
    original_replace = Path.replace

    def record_replace(source: Path, target: Path) -> Path:
        seen["source"] = source
        seen["target"] = target
        seen["mode_at_replace"] = stat.S_IMODE(source.stat().st_mode)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", record_replace)

    quotas._kimi_save_credentials(credential, {"access_token": "saved"})

    assert seen["source"].parent == credential.parent
    assert seen["source"] != credential
    assert seen["source"].name.startswith(f".{credential.name}.")
    assert seen["target"] == credential
    assert seen["mode_at_replace"] == 0o600
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert json.loads(credential.read_text(encoding="utf-8")) == {"access_token": "saved"}


def test_expired_token_without_a_refresh_token_is_unsupported(monkeypatch, credential) -> None:
    expired = json.loads(credential.read_text(encoding="utf-8"))
    expired.pop("refresh_token")
    expired["expires_at"] = int(time.time()) - 1
    credential.write_text(json.dumps(expired), encoding="utf-8")

    result = quotas.kimi_probe()

    assert result == {
        "status": "unsupported",
        "reason": "Kimi token expired — run any `kimi` command to refresh it",
    }


def test_no_credential_is_unsupported_not_an_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi-code"))

    result = quotas.kimi_probe()

    assert result["status"] == "unsupported"
    assert "no Kimi Code credential" in result["reason"]


def test_a_reshaped_payload_is_unsupported_rather_than_an_error(monkeypatch, credential) -> None:
    _respond(monkeypatch, {"usage": {"limit": "100"}, "limits": []})

    result = quotas.kimi_probe()

    assert result["status"] == "unsupported"
    assert "unrecognised" in result["reason"]


def test_a_truthy_non_object_user_is_unsupported_not_an_error(monkeypatch, credential) -> None:
    payload = json.loads(json.dumps(_PAYLOAD))
    payload["user"] = "gap"
    _respond(monkeypatch, payload)

    result = quotas.kimi_probe()

    assert result["status"] == "unsupported"
    assert "unrecognised" in result["reason"]

"""Manus API v2 credit balance probe."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas
from playmaker.cli import _render_provider

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "manus_available_credits.json").read_text(
        encoding="utf-8"
    )
)


def _respond(monkeypatch, payload: dict | Exception) -> dict:
    seen: dict = {}

    def fake(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        if isinstance(payload, Exception):
            raise payload
        return copy.deepcopy(payload)

    monkeypatch.setattr(quotas, "_http_json", fake)
    return seen


def test_manus_is_registered() -> None:
    assert "manus" in quotas.PROBES
    assert quotas.PROBES["manus"] is quotas.manus_probe


def test_fixture_yields_monthly_window_and_note(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "manus-secret")
    seen = _respond(monkeypatch, FIXTURE)

    result = quotas.manus_probe()

    assert (result["status"], result["windows"][0]["name"]) == ("ok", "Monthly")
    assert result["windows"][0]["pct_left"] == round(9128 / 24000 * 100) == 38
    assert result["windows"][0]["reset_at_iso"] == quotas._epoch_to_iso(
        int(FIXTURE["current_period_end"])
    )
    assert result["note"] == "13 258 credits (9 128 monthly + 4 130 add-on)"
    assert seen["url"] == "https://api.manus.ai/v2/usage.availableCredits"
    assert seen["kwargs"]["headers"] == {"x-manus-api-key": "manus-secret"}
    assert seen["kwargs"]["timeout"] == 15.0


def test_documented_data_wrapper_and_digit_strings_match(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "manus-secret")
    wrapped = {"data": {key: str(value) if isinstance(value, int) else value
                        for key, value in FIXTURE.items()}}
    _respond(monkeypatch, wrapped)

    result = quotas.manus_probe()

    assert (
        result["status"],
        result["windows"][0]["pct_left"],
        result["note"],
    ) == ("ok", 38, "13 258 credits (9 128 monthly + 4 130 add-on)")


def test_no_key_is_unsupported_with_add_command(monkeypatch) -> None:
    monkeypatch.delenv("MANUS_API_KEY", raising=False)
    monkeypatch.setattr(quotas.sys, "platform", "linux")

    result = quotas.manus_probe()

    expected = {
        "status": "unsupported",
        "reason": (
            "no Manus API key — add one: security add-generic-password "
            "-s playmaker-manus-api -a playmaker -w"
        ),
    }
    assert result == expected


def test_whitespace_env_key_falls_through_to_keychain(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "  \t ")
    monkeypatch.setattr(quotas.sys, "platform", "darwin")
    calls = []

    def no_keychain_key(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

    monkeypatch.setattr(quotas.subprocess, "run", no_keychain_key)

    result = quotas.manus_probe()

    assert calls
    assert result["status"] == "unsupported"


def test_keychain_key_is_used(monkeypatch) -> None:
    monkeypatch.delenv("MANUS_API_KEY", raising=False)
    monkeypatch.setattr(quotas.sys, "platform", "darwin")

    def fake_run(cmd, **kwargs):
        assert cmd == [
            "security", "find-generic-password", "-s", "playmaker-manus-api",
            "-a", "playmaker", "-w",
        ]
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(cmd, 0, stdout="keychain-secret\n", stderr="")

    monkeypatch.setattr(quotas.subprocess, "run", fake_run)
    seen = _respond(monkeypatch, FIXTURE)

    assert quotas.manus_probe()["status"] == "ok"
    assert seen["kwargs"]["headers"]["x-manus-api-key"] == "keychain-secret"


def test_rejected_key_and_other_errors_never_leak_secret(monkeypatch) -> None:
    secret = "manus-LEAK-ME"
    monkeypatch.setenv("MANUS_API_KEY", secret)
    _respond(monkeypatch, RuntimeError(f"HTTP 401 endpoint: {secret}"))

    rejected = quotas.manus_probe()

    assert rejected["status"] == "error"
    assert "rejected" in rejected["error"]
    assert secret not in json.dumps(rejected)

    _respond(monkeypatch, RuntimeError(f"connection failed: {secret}"))
    failed = quotas.manus_probe()
    assert failed == {"status": "error", "error": "Manus API request failed"}
    assert secret not in json.dumps(failed)


def test_zero_or_missing_monthly_cap_omits_window(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "manus-secret")
    payload = copy.deepcopy(FIXTURE)
    payload["pro_monthly_credits"] = 0
    _respond(monkeypatch, payload)

    assert quotas.manus_probe()["windows"] == []


def test_payload_without_documented_credit_fields_is_an_error(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "manus-secret")
    _respond(monkeypatch, {"ok": True, "request_id": "req-redacted"})

    assert quotas.manus_probe() == {
        "status": "error",
        "error": "unexpected Manus credits payload",
    }


def test_missing_or_invalid_period_end_has_no_reset_or_forecast(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "manus-secret")
    for period_end in (None, "not-an-epoch"):
        payload = copy.deepcopy(FIXTURE)
        if period_end is None:
            payload.pop("current_period_end")
        else:
            payload["current_period_end"] = period_end
        _respond(monkeypatch, payload)

        window = quotas.manus_probe()["windows"][0]

        assert window["reset_at_iso"] is None
        assert window["reset_relative"] is None
        assert window["forecast"] is None


def test_top_level_credits_take_precedence_over_data(monkeypatch) -> None:
    monkeypatch.setenv("MANUS_API_KEY", "manus-secret")
    payload = copy.deepcopy(FIXTURE)
    payload["data"] = {
        "total_credits": 1,
        "periodic_credits": 1,
        "addon_credits": 0,
        "pro_monthly_credits": 100,
    }
    _respond(monkeypatch, payload)

    result = quotas.manus_probe()

    assert result["windows"][0]["pct_left"] == 38
    assert result["note"] == "13 258 credits (9 128 monthly + 4 130 add-on)"


def test_renderer_shows_manus_title(monkeypatch) -> None:
    rendered: list[str] = []
    monkeypatch.setattr("playmaker.cli.console.print", rendered.append)

    _render_provider("manus", {"status": "ok", "windows": [], "note": "credits"})

    assert "Manus" in rendered[0]
    assert "bright_magenta" in rendered[0]

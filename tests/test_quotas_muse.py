"""Muse Code subscription quota probe."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas
from playmaker import __version__

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "muse_key_response.json").read_text(
        encoding="utf-8"
    )
)


def _auth_file(monkeypatch, tmp_path, payload: dict | None) -> Path:
    """Point the probe at a scratch auth.json; None means no file at all."""
    monkeypatch.setenv("MUSE_AUTH_PATH", str(tmp_path / "auth.json"))
    if payload is not None:
        (tmp_path / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path / "auth.json"


def _inline(token: str) -> dict:
    return {"schema_version": 1, "providers": {"meta": {"access_token": token}}}


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _keychain(monkeypatch, secret: dict | None, *, fail_read=None):
    """Fake `security`: existence check, then the `-w` read. Returns the calls."""
    monkeypatch.setattr(quotas.sys, "platform", "darwin")
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if "-w" not in cmd:
            if secret is None:
                return _Completed(returncode=1, stderr="not found")
            return _Completed(returncode=0, stdout="")
        if fail_read == "timeout":
            raise subprocess.TimeoutExpired(cmd, 30)
        if fail_read == "denied":
            return _Completed(returncode=1, stderr="denied")
        return _Completed(returncode=0, stdout=json.dumps(secret))

    monkeypatch.setattr(quotas.subprocess, "run", fake_run)
    return calls


def _respond(monkeypatch, payload: dict | Exception) -> dict:
    seen: dict = {}

    def fake(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        seen["headers"] = kwargs.get("headers") or {}
        if isinstance(payload, Exception):
            raise payload
        return copy.deepcopy(payload)

    monkeypatch.setattr(quotas, "_http_json", fake)
    return seen


def test_muse_is_registered() -> None:
    assert quotas.PROBES["muse"] is quotas.muse_probe


def test_inline_token_wins_over_the_keychain(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:inline-token"))

    def no_keychain(cmd, **kwargs):
        raise AssertionError("keychain must not be consulted")

    monkeypatch.setattr(quotas.subprocess, "run", no_keychain)
    seen = _respond(monkeypatch, FIXTURE)

    result = quotas.muse_probe()

    assert result["status"] == "ok"
    assert seen["headers"]["Authorization"] == "Bearer dca:inline-token"


def test_keychain_token_is_used_when_auth_json_has_none(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, {"providers": {"meta": {"mechanism": "oauth"}}})
    calls = _keychain(monkeypatch, {"access_token": "dca:keychain-token"})
    seen = _respond(monkeypatch, FIXTURE)

    result = quotas.muse_probe()

    assert result["status"] == "ok"
    assert seen["headers"]["Authorization"] == "Bearer dca:keychain-token"
    # Existence check first (no secret), then the `-w` read.
    assert len(calls) == 2
    assert "-w" not in calls[0][0]
    assert "-w" in calls[1][0]
    assert calls[0][0][:4] == ["security", "find-generic-password", "-s",
                               "ai.meta.dev.credentials"]
    assert calls[1][1].get("timeout") == 30


def test_keychain_beats_a_non_device_code_inline_token(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("sk-not-device-code"))
    _keychain(monkeypatch, {"access_token": "dca:keychain-token"})
    seen = _respond(monkeypatch, FIXTURE)

    result = quotas.muse_probe()

    assert result["status"] == "ok"
    assert seen["headers"]["Authorization"] == "Bearer dca:keychain-token"


def test_the_keychain_is_never_consulted_off_macos(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, None)
    monkeypatch.setattr(quotas.sys, "platform", "linux")

    def no_security(cmd, **kwargs):
        raise AssertionError("`security` must not run off macOS")

    monkeypatch.setattr(quotas.subprocess, "run", no_security)

    result = quotas.muse_probe()

    assert result["status"] == "unsupported"
    assert "muse login" in result["reason"]


def test_a_failed_keychain_read_leaks_no_process_output(
    monkeypatch, tmp_path
) -> None:
    leaked = "dca:LEAK-READ-7b3f9d"
    _auth_file(monkeypatch, tmp_path, None)
    monkeypatch.setattr(quotas.sys, "platform", "darwin")

    def fake_run(cmd, **kwargs):
        if "-w" not in cmd:
            return _Completed(returncode=0, stdout="")
        return _Completed(returncode=1, stdout=leaked, stderr=f"error: {leaked}")

    monkeypatch.setattr(quotas.subprocess, "run", fake_run)

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert leaked not in json.dumps(result)


def test_a_hung_existence_check_is_an_error_not_not_configured(
    monkeypatch, tmp_path
) -> None:
    _auth_file(monkeypatch, tmp_path, None)
    monkeypatch.setattr(quotas.sys, "platform", "darwin")

    def fake_run(cmd, **kwargs):
        if "-w" not in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        raise AssertionError("the `-w` read must not run after a hung check")

    monkeypatch.setattr(quotas.subprocess, "run", fake_run)

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "timed out" in result["error"]
    assert "muse login" in result["error"]


def test_an_unrunnable_security_is_an_error_but_a_missing_one_is_not(
    monkeypatch, tmp_path
) -> None:
    _auth_file(monkeypatch, tmp_path, None)
    monkeypatch.setattr(quotas.sys, "platform", "darwin")

    def denied(cmd, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(quotas.subprocess, "run", denied)
    result = quotas.muse_probe()
    assert result["status"] == "error"
    assert "muse login" in result["error"]

    def missing(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(quotas.subprocess, "run", missing)
    assert quotas.muse_probe()["status"] == "unsupported"


def test_no_login_is_unsupported_not_an_error(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, None)
    _keychain(monkeypatch, None)

    result = quotas.muse_probe()

    assert result["status"] == "unsupported"
    assert "muse login" in result["reason"]


def test_a_token_without_the_device_code_prefix_is_an_error(
    monkeypatch, tmp_path
) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("sk-not-device-code"))
    _keychain(monkeypatch, None)

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "device-code" in result["error"]
    assert "muse login" in result["error"]


def test_a_denied_keychain_read_is_an_error_with_a_hint(
    monkeypatch, tmp_path
) -> None:
    _auth_file(monkeypatch, tmp_path, None)
    _keychain(monkeypatch, {"access_token": "dca:keychain-token"}, fail_read="denied")

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "security" in result["error"]
    assert "muse login" in result["error"]


def test_a_keychain_timeout_is_an_error_with_a_hint(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, None)
    _keychain(monkeypatch, {"access_token": "dca:keychain-token"}, fail_read="timeout")

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "security" in result["error"]
    assert "muse login" in result["error"]


def test_the_key_request_shape(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:request-shape"))
    seen = _respond(monkeypatch, FIXTURE)

    quotas.muse_probe()

    assert seen["url"] == "https://api.meta.ai/muse-code/key"
    assert seen["kwargs"]["method"] == "POST"
    assert seen["kwargs"]["body"] == {}
    assert seen["kwargs"]["timeout"] == 15.0
    assert seen["headers"]["Authorization"] == "Bearer dca:request-shape"
    assert seen["headers"]["x-api-version"] == "1.0.0"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["headers"]["User-Agent"] == f"playmaker/{__version__}"


def test_fixture_yields_session_and_weekly(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:fixture"))
    _respond(monkeypatch, FIXTURE)

    result = quotas.muse_probe()

    assert result["status"] == "ok"
    assert result["tier"] == "Muse Code Power Usage"
    assert result["account_email"] == FIXTURE["user_email"]
    assert [(w["name"], w["pct_left"]) for w in result["windows"]] == [
        ("Session", 100),
        ("Weekly", 97),
    ]
    session, weekly = result["windows"]
    assert session["reset_at_iso"] == quotas._epoch_to_iso(1790775892)
    assert weekly["reset_at_iso"] == quotas._epoch_to_iso(1791158400)
    assert session["reset_relative"]
    assert weekly["forecast"] == "Lasts until reset"
    assert set(session) == {
        "name", "pct_left", "reset_at_iso", "reset_relative", "forecast", "reserve_pct",
    }


def test_an_idle_window_reports_a_note_instead_of_numbers(
    monkeypatch, tmp_path
) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:idle"))
    payload = copy.deepcopy(FIXTURE)
    del payload["subs_usage"]
    _respond(monkeypatch, payload)

    result = quotas.muse_probe()

    assert result["status"] == "ok"
    assert result["windows"] == []
    assert result["tier"] == "Muse Code Power Usage"
    assert result["note"] == (
        "5-hour window idle — Meta reports no usage until the next prompt; weekly unknown"
    )


def test_a_null_subs_usage_is_idle_too(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:idle-null"))
    payload = copy.deepcopy(FIXTURE)
    payload["subs_usage"] = None
    _respond(monkeypatch, payload)

    result = quotas.muse_probe()

    assert result["status"] == "ok"
    assert result["windows"] == []
    assert "idle" in result["note"]


def test_a_pay_as_you_go_account_has_no_windows(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:payg"))
    payload = copy.deepcopy(FIXTURE)
    payload["is_subs_active"] = False
    _respond(monkeypatch, payload)

    result = quotas.muse_probe()

    assert result["status"] == "unsupported"
    assert "pay-as-you-go" in result["reason"]


def test_incomplete_billing_is_an_error(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:billing"))
    payload = copy.deepcopy(FIXTURE)
    payload["require_payment"] = True
    _respond(monkeypatch, payload)

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "billing incomplete" in result["error"]
    assert "https://dev.meta.ai" in result["error"]


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_login_names_muse_login(monkeypatch, tmp_path, code) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:rejected"))
    _respond(monkeypatch, RuntimeError(f"HTTP {code} https://api.meta.ai/muse-code/key"))

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "muse login" in result["error"]


def test_rate_limiting_is_an_error(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:limited"))
    _respond(monkeypatch, RuntimeError("HTTP 429 https://api.meta.ai/muse-code/key"))

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "rate limited" in result["error"]


def test_a_server_error_carries_its_status(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:server"))
    _respond(monkeypatch, RuntimeError("HTTP 500 https://api.meta.ai/muse-code/key"))

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "HTTP 500" in result["error"]


def test_an_unexpected_status_carries_its_status(monkeypatch, tmp_path) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:teapot"))
    _respond(
        monkeypatch,
        RuntimeError('HTTP 418 https://api.meta.ai/muse-code/key: {"title": "teapot"}'),
    )

    result = quotas.muse_probe()

    assert result["status"] == "error"
    assert "HTTP 418" in result["error"]
    assert "teapot" in result["error"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda usage: usage.pop("window"),
        lambda usage: usage.pop("weekly"),
        lambda usage: usage["window"].update({"used_percent": "lots"}),
        lambda usage: usage["weekly"].update({"used_percent": None}),
        lambda usage: usage["window"].pop("window_duration_mins"),
    ],
    ids=["no-window", "no-weekly", "text-percent", "null-percent", "no-duration"],
)
def test_a_malformed_payload_is_an_error_without_a_body_echo(
    monkeypatch, tmp_path, mutate
) -> None:
    _auth_file(monkeypatch, tmp_path, _inline("dca:malformed"))
    payload = copy.deepcopy(FIXTURE)
    mutate(payload["subs_usage"])
    _respond(monkeypatch, payload)

    result = quotas.muse_probe()

    assert result == {"status": "error", "error": "unexpected Muse usage payload"}


def test_secrets_stay_out_of_results_and_messages(monkeypatch, tmp_path) -> None:
    token = "dca:LEAK-TOKEN-4f8e2a"
    api_key = "LLM|LEAK-APIKEY-9c1d7b"
    payment = "pm_LEAK-PAY-6e5f4a"
    _auth_file(monkeypatch, tmp_path, _inline(token))
    _keychain(monkeypatch, {"access_token": token, "api_key": api_key})

    payload = copy.deepcopy(FIXTURE)
    payload["api_key"] = api_key
    payload["payment_method"] = payment

    surfaces: list[str] = []

    def collect(result: dict) -> None:
        surfaces.append(json.dumps(result))

    _respond(monkeypatch, payload)
    collect(quotas.muse_probe())

    idle = copy.deepcopy(payload)
    del idle["subs_usage"]
    _respond(monkeypatch, idle)
    collect(quotas.muse_probe())

    billing = copy.deepcopy(payload)
    billing["require_payment"] = True
    _respond(monkeypatch, billing)
    collect(quotas.muse_probe())

    broken = copy.deepcopy(payload)
    broken["subs_usage"].pop("weekly")
    _respond(monkeypatch, broken)
    collect(quotas.muse_probe())

    _respond(
        monkeypatch,
        RuntimeError(
            f'HTTP 401 https://api.meta.ai/muse-code/key: {{"detail": "denied",'
            f' "key": "{api_key}"}}'
        ),
    )
    try:
        collect(quotas.muse_probe())
    except Exception as exc:  # noqa: BLE001 — the point is what the message holds
        surfaces.append(str(exc))
        surfaces.append(repr(exc))

    combined = "\n".join(surfaces)
    assert token not in combined
    assert api_key not in combined
    assert payment not in combined

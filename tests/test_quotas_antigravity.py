from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas


# Shape returned by agy's localhost RetrieveUserQuotaSummary endpoint.
_SUMMARY = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {"bucketId": "gemini-weekly", "displayName": "Weekly Limit",
                 "remainingFraction": 0.9867, "resetTime": "2026-07-19T13:26:30Z"},
                {"bucketId": "gemini-5h", "displayName": "Five Hour Limit",
                 "remainingFraction": 0.9703, "resetTime": "2026-07-12T18:26:30Z"},
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {"bucketId": "3p-weekly", "displayName": "Weekly Limit",
                 "remainingFraction": 0.9583, "resetTime": "2026-07-19T13:21:50Z"},
                {"bucketId": "3p-5h", "displayName": "Five Hour Limit",
                 "remainingFraction": 0.8995, "resetTime": "2026-07-12T18:21:50Z"},
                # disabled buckets are dropped
                {"bucketId": "3p-disabled", "displayName": "Off", "disabled": True,
                 "remainingFraction": 0.0},
            ],
        },
    ]
}


def test_windows_from_summary_categorized_and_ordered() -> None:
    windows = quotas._antigravity_windows_from_summary(_SUMMARY)

    # 5h precedes weekly within each group; groups keep source order.
    assert [w["name"] for w in windows] == [
        "Gemini 5h",
        "Gemini weekly",
        "Claude/GPT 5h",
        "Claude/GPT weekly",
    ]
    by_name = {w["name"]: w for w in windows}
    assert by_name["Gemini 5h"]["pct_left"] == 97
    assert by_name["Claude/GPT weekly"]["pct_left"] == 96
    assert by_name["Gemini weekly"]["reset_at_iso"] == "2026-07-19T13:26:30Z"


def test_group_and_bucket_classifiers() -> None:
    assert quotas._antigravity_group_short("Gemini Models") == "Gemini"
    assert quotas._antigravity_group_short("Claude and GPT models") == "Claude/GPT"
    assert quotas._antigravity_bucket_window("gemini-5h", "Five Hour Limit") == ("5h", 5 * 3600)
    assert quotas._antigravity_bucket_window("3p-weekly", "Weekly Limit") == ("weekly", 7 * 86400)
    assert quotas._antigravity_bucket_window("unknown", "Something") is None


def test_antigravity_probe_prefers_local(monkeypatch) -> None:
    monkeypatch.setattr(quotas, "_antigravity_daemon_ports", lambda: [51024])
    monkeypatch.setattr(quotas, "_antigravity_local_summary", lambda ports, timeout=5.0: _SUMMARY)
    monkeypatch.setattr(quotas, "_antigravity_account_meta", lambda: {"email": "x@y.z", "tier": "Paid"})

    result = quotas.antigravity_probe()

    assert result["source"] == "local"
    assert result["tier"] == "Paid"
    assert result["account_email"] == "x@y.z"
    assert len(result["windows"]) == 4


def test_antigravity_probe_falls_back_to_remote(monkeypatch) -> None:
    monkeypatch.setattr(quotas, "_antigravity_daemon_ports", lambda: [])
    called = {}

    def fake_remote(base_url, ide_type):
        called["base_url"] = base_url
        return {"status": "ok", "windows": [{"name": "Flash", "pct_left": 100}]}

    monkeypatch.setattr(quotas, "_google_code_assist_probe", fake_remote)

    result = quotas.antigravity_probe()

    assert result["source"] == "remote"
    assert called["base_url"] == quotas._ANTIGRAVITY_CLOUDCODE_BASE


def test_local_summary_returns_none_when_no_port_answers(monkeypatch) -> None:
    # No daemon reachable → helper must return None (so probe falls back).
    def boom(req, timeout=None, context=None):
        raise OSError("connection refused")

    monkeypatch.setattr(quotas.urllib.request, "urlopen", boom)

    assert quotas._antigravity_local_summary([49999]) is None

from __future__ import annotations

import http.client
import json
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
    monkeypatch.setattr(
        quotas, "_antigravity_account_meta", lambda: {"email": "x@y.z", "tier": "Paid"}
    )

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


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_local_summary_skips_a_port_that_does_not_speak_http(monkeypatch) -> None:
    # A TLS-only listener answers a plaintext POST with a TLS alert record;
    # urllib surfaces that as http.client.BadStatusLine, which is not an
    # OSError. It disqualifies that port — it must not sink the probe.
    tls_alert = "\x15\x03\x03\x00\x02\x022"

    def urlopen(req, timeout=None, context=None):
        if ":50652/" in req.full_url:
            if req.full_url.startswith("https"):
                raise TimeoutError("The read operation timed out")
            raise http.client.BadStatusLine(tls_alert)
        return _Response(json.dumps({"response": _SUMMARY}).encode())

    monkeypatch.setattr(quotas.urllib.request, "urlopen", urlopen)

    assert quotas._antigravity_local_summary([50652, 51802]) == _SUMMARY


def test_local_summary_ignores_a_port_that_answers_non_object_json(monkeypatch) -> None:
    def urlopen(req, timeout=None, context=None):
        return _Response(b"[]")

    monkeypatch.setattr(quotas.urllib.request, "urlopen", urlopen)

    assert quotas._antigravity_local_summary([49999]) is None


def test_daemon_ports_lists_only_the_daemon_sockets(monkeypatch) -> None:
    # lsof ORs its selectors unless told otherwise, so `-p PID -iTCP` without
    # `-a` is every LISTEN socket on the machine — that is how the probe ended
    # up POSTing to Steam and chromedriver. The lookup must AND them and ask
    # for exactly the pids pgrep found.
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "pgrep":
            return _Proc("8577\n" if "agy" in cmd[-1] else "")
        assert cmd[0] == "lsof"
        return _Proc(
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "agy 8577 me 10u IPv4 0x1 0t0 TCP 127.0.0.1:51802 (LISTEN)\n"
            "agy 8577 me 11u IPv4 0x2 0t0 TCP 127.0.0.1:51803 (LISTEN)\n"
        )

    monkeypatch.setattr(quotas.subprocess, "run", run)

    assert quotas._antigravity_daemon_ports() == [51802, 51803]
    lsof = [c for c in calls if c[0] == "lsof"]
    assert len(lsof) == 1
    assert "-a" in lsof[0]
    assert lsof[0][lsof[0].index("-p") + 1] == "8577"


def test_daemon_ports_is_empty_without_a_daemon(monkeypatch) -> None:
    calls: list[list[str]] = []

    class _Proc:
        stdout = ""
        returncode = 1

    def run(cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(quotas.subprocess, "run", run)

    assert quotas._antigravity_daemon_ports() == []
    # No pids → no lsof at all (an unfiltered lsof is the whole-machine listing).
    assert all(c[0] == "pgrep" for c in calls)


def test_antigravity_probe_falls_back_to_remote_when_the_local_path_blows_up(
    monkeypatch,
) -> None:
    def boom():
        raise http.client.BadStatusLine("\x15\x03\x03\x00\x02\x022")

    monkeypatch.setattr(quotas, "_antigravity_daemon_ports", boom)
    remote = {"status": "ok", "windows": [{"name": "Flash", "pct_left": 100}]}
    monkeypatch.setattr(quotas, "_google_code_assist_probe", lambda base_url, ide_type: remote)

    result = quotas.antigravity_probe()

    assert result["status"] == "ok"
    assert result["source"] == "remote"

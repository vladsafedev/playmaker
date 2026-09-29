from __future__ import annotations

import http.client
import json
import re
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import playmaker.quotas as quotas
from playmaker import cli

# The real spawner, kept before the autouse fixture below swaps it out.
_REAL_SPAWNED_SUMMARY = quotas._antigravity_spawned_summary


@pytest.fixture(autouse=True)
def _never_start_a_real_agy(monkeypatch) -> None:
    # A probe test that reaches the spawn path must not launch the user's agy.
    def refuse(budget: float = 0.0) -> dict:
        raise RuntimeError("spawning agy is disabled in tests")

    monkeypatch.setattr(quotas, "_antigravity_spawned_summary", refuse)

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
    # Why the rich view is missing travels with the coarse one.
    assert result["local_error"] == "spawning agy is disabled in tests"


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


# ---- agy 1.2: CSRF-guarded daemon, own short-lived daemon, unlicensed remote --


def test_proc_patterns_match_agy_1_2_command_lines() -> None:
    agy = quotas._ANTIGRAVITY_PROC_PATTERNS[0]
    # A wrapper `agy` execs agy-real with argv[0] "agy"; direct starts carry the path.
    assert re.search(agy, "agy -p hi --model gemini-3.8-flash-low")
    assert re.search(agy, "/Users/me/.local/bin/agy-real --csrf_token=abc")
    assert re.search(agy, "/Users/me/.local/bin/agy")
    assert re.search(agy, "/usr/bin/python3 /Users/me/.local/bin/agy")
    # Arguments that merely mention agy are not a daemon.
    assert not re.search(agy, "/usr/bin/python3 /opt/bin/playmaker dispatch agy -p x")
    assert not re.search(agy, "sh -c tail --log-file /tmp/playmaker-agy-1.log")


def test_local_summary_sends_the_csrf_token_only_when_it_has_one(monkeypatch) -> None:
    seen: list[str | None] = []

    def urlopen(req, timeout=None, context=None):
        seen.append(req.get_header(quotas._ANTIGRAVITY_CSRF_HEADER.capitalize()))
        return _Response(json.dumps({"response": _SUMMARY}).encode())

    monkeypatch.setattr(quotas.urllib.request, "urlopen", urlopen)

    assert quotas._antigravity_local_summary([60003], token="t0k") == _SUMMARY
    assert quotas._antigravity_local_summary([60003]) == _SUMMARY
    assert seen == ["t0k", None]


class _FakeAgy:
    """Stands in for subprocess.Popen(agy ...): alive until stopped."""

    started: list[_FakeAgy] = []

    def __init__(self, argv, **kwargs) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode: int | None = None
        self.stdin_closed = False
        self.stdin = self
        _FakeAgy.started.append(self)

    def close(self) -> None:  # stdin
        self.stdin_closed = True

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout=None) -> int:
        self.returncode = -15 if self.returncode is None else self.returncode
        return self.returncode

    @property
    def token(self) -> str:
        return next(a for a in self.argv if a.startswith("--csrf_token=")).split("=", 1)[1]


@pytest.fixture
def fake_agy(monkeypatch):
    _FakeAgy.started = []
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(quotas.shutil, "which", lambda name: "/opt/bin/agy")
    monkeypatch.setattr(quotas.subprocess, "Popen", _FakeAgy)
    monkeypatch.setattr(quotas.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(quotas.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(quotas, "_listening_ports", lambda pids: [60003, 60004])
    return signals


def test_spawned_summary_starts_a_headless_agy_with_its_own_token_and_stops_it(
    monkeypatch, fake_agy
) -> None:
    # The first answer after start-up is the "not logged in" 500 (→ None);
    # the probe keeps asking, with the token it started agy with.
    answers = iter([None, _SUMMARY])
    tokens: list[str | None] = []

    def local_summary(ports, timeout=5.0, token=None):
        tokens.append(token)
        return next(answers)

    monkeypatch.setattr(quotas, "_antigravity_local_summary", local_summary)

    assert _REAL_SPAWNED_SUMMARY(budget=5) == _SUMMARY

    [agy] = _FakeAgy.started
    assert agy.argv[0] == "/opt/bin/agy"
    # No TTY needed: stream-json print mode waits on stdin instead of quitting.
    assert agy.argv[agy.argv.index("--input-format") + 1] == "stream-json"
    assert agy.argv[-1] == "-p="
    assert agy.kwargs["start_new_session"] is True
    assert tokens == [agy.token, agy.token]
    # Stopped as a group once the summary is in.
    assert agy.stdin_closed
    assert fake_agy == [(4242, signal.SIGTERM)]


def test_spawned_summary_gives_up_after_its_budget_and_still_stops_agy(
    monkeypatch, fake_agy
) -> None:
    clock = iter(range(1000))
    monkeypatch.setattr(quotas.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(quotas, "_antigravity_local_summary", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="did not report quota within 3s"):
        _REAL_SPAWNED_SUMMARY(budget=3)

    assert fake_agy == [(4242, signal.SIGTERM)]


def test_spawned_summary_reports_an_agy_that_exits_early(monkeypatch, fake_agy) -> None:
    class _DeadAgy(_FakeAgy):
        def __init__(self, argv, **kwargs) -> None:
            super().__init__(argv, **kwargs)
            self.returncode = 2

    monkeypatch.setattr(quotas.subprocess, "Popen", _DeadAgy)

    with pytest.raises(RuntimeError, match="agy exited with code 2"):
        _REAL_SPAWNED_SUMMARY(budget=5)


def test_spawned_summary_needs_agy_on_path(monkeypatch) -> None:
    monkeypatch.setattr(quotas.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="not on PATH"):
        _REAL_SPAWNED_SUMMARY(budget=5)


def test_stop_escalates_to_sigkill_when_agy_ignores_sigterm(monkeypatch) -> None:
    signals: list[int] = []
    monkeypatch.setattr(quotas.os, "killpg", lambda pid, sig: signals.append(sig))

    class _Stubborn(_FakeAgy):
        def wait(self, timeout=None) -> int:
            if signals[-1] == signal.SIGTERM:
                raise quotas.subprocess.TimeoutExpired("agy", timeout)
            return super().wait(timeout)

    quotas._antigravity_stop(_Stubborn(["agy"]))

    assert signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("running_ports", [[], [51024]])
def test_antigravity_probe_starts_its_own_daemon_when_none_answers(
    monkeypatch, running_ports
) -> None:
    # A running `agy -p` listens but 401s without its token (→ None);
    # with no agy running at all there is nobody to ask.
    monkeypatch.setattr(quotas, "_antigravity_daemon_ports", lambda: running_ports)
    monkeypatch.setattr(quotas, "_antigravity_local_summary", lambda ports, timeout=5.0: None)
    monkeypatch.setattr(quotas, "_antigravity_spawned_summary", lambda: _SUMMARY)
    monkeypatch.setattr(quotas, "_antigravity_account_meta", lambda: {"email": "x@y.z"})

    def remote(base_url, ide_type):
        raise AssertionError("the remote must not be asked when the daemon answered")

    monkeypatch.setattr(quotas, "_google_code_assist_probe", remote)

    result = quotas.antigravity_probe()

    assert result["status"] == "ok"
    assert result["source"] == "local"
    assert [w["name"] for w in result["windows"]][:2] == ["Gemini 5h", "Gemini weekly"]


# Verbatim head of the 403 retrieveUserQuota began answering in 2026-09.
_UNLICENSED = RuntimeError(
    "HTTP 403 https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota: {\n"
    '  "error": {\n    "code": 403,\n'
    '    "message": "You do not have a valid license of this product. Please contact '
    'your administrator to request a license.'
)


def _local_path_down(monkeypatch, reason: str) -> None:
    monkeypatch.setattr(quotas, "_antigravity_daemon_ports", lambda: [])

    def spawned():
        raise RuntimeError(reason)

    monkeypatch.setattr(quotas, "_antigravity_spawned_summary", spawned)


def test_unlicensed_remote_makes_the_lane_unavailable_not_an_error(monkeypatch) -> None:
    _local_path_down(monkeypatch, "agy did not report quota within 12s")

    def remote(base_url, ide_type):
        raise _UNLICENSED

    monkeypatch.setattr(quotas, "_google_code_assist_probe", remote)
    monkeypatch.setattr(quotas, "_antigravity_account_meta", lambda: {"email": "x@y.z"})

    result = quotas.antigravity_probe()

    assert result["status"] == "unavailable"
    assert result["account_email"] == "x@y.z"
    assert "no valid license" in result["reason"]
    assert "agy did not report quota within 12s" in result["hint"]
    assert "playmaker quotas --refresh" in result["hint"]


def test_other_remote_failures_are_still_errors(monkeypatch) -> None:
    _local_path_down(monkeypatch, "agy is not on PATH")

    def remote(base_url, ide_type):
        raise RuntimeError("HTTP 500 https://daily-cloudcode-pa.googleapis.com: boom")

    monkeypatch.setattr(quotas, "_google_code_assist_probe", remote)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        quotas.antigravity_probe()


def test_refresh_keeps_an_unavailable_lane_and_its_last_success(monkeypatch, tmp_path) -> None:
    _local_path_down(monkeypatch, "agy is not on PATH")

    def remote(base_url, ide_type):
        raise _UNLICENSED

    monkeypatch.setattr(quotas, "_google_code_assist_probe", remote)
    monkeypatch.setattr(quotas, "_antigravity_account_meta", lambda: {})
    monkeypatch.setattr(quotas, "PROBES", {"agy": quotas.antigravity_probe})
    path = tmp_path / "quotas.json"
    path.write_text(
        json.dumps({"providers": {"agy": {"last_success": "2026-09-24T10:00:00+00:00"}}}),
        encoding="utf-8",
    )

    agy = quotas.refresh_all(path)["providers"]["agy"]

    assert agy["status"] == "unavailable"
    assert agy["last_success"] == "2026-09-24T10:00:00+00:00"

    with cli.console.capture() as capture:
        cli._render_provider("agy", agy)
    text = capture.get()
    assert "unavailable" in text
    assert "hint:" in text
    assert "last success: 2026-09-24" in text
    assert "error" not in text


def test_remote_fallback_says_why_the_daemon_was_missed() -> None:
    with cli.console.capture() as capture:
        cli._render_provider(
            "agy",
            {
                "status": "ok",
                "source": "remote",
                "local_error": "agy is not on PATH",
                "windows": [{"name": "Flash", "pct_left": 100}],
            },
        )
    text = capture.get()
    assert "Gemini-only (daemon offline)" in text
    assert "local daemon: agy is not on PATH" in text

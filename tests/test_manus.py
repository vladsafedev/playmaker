from __future__ import annotations

import base64
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker.agents import manus

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "secret-that-must-not-leak"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _events(name: str):
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("MANUS_API_KEY", KEY)
    monkeypatch.setattr(manus.state, "PLAYMAKER_HOME", tmp_path / "state")
    monkeypatch.setattr(
        manus,
        "agent_setting",
        lambda agent, key, default=None: {"poll_seconds": 0, "max_wait_minutes": 30}.get(
            key, default
        ),
    )


def test_dispatch_capture_returns_result_and_early_id(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    created = _fixture("manus_create.json")
    events = _events("manus_events_lite.jsonl")
    detail = _fixture("manus_detail_lite.json")
    calls = []

    def request(method, path, key, body=None, *, query=None):
        calls.append((method, path, body, query))
        if path.endswith("task.create"):
            return created
        if path.endswith("listMessages"):
            assert started == [created["task_id"]]
            return {"ok": True, "messages": events, "has_more": False}
        if path.endswith("task.detail"):
            return detail
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    started = []
    result = handler.dispatch(
        "Exact prompt", tmp_path, on_session_started=started.append, model="lite"
    )

    expected = next(
        event["assistant_message"]["content"]
        for event in events
        if event.get("assistant_message", {}).get("delivery_kind") == "result"
    )
    assert started == [created["task_id"]]
    assert result.agent_session_id == created["task_id"]
    assert result.initial_output == expected
    assert result.exit_code == 0
    create_body = calls[0][2]
    assert create_body["message"]["content"] == [{"type": "text", "text": "Exact prompt"}]
    assert len(create_body["message"]["content"]) == 1
    assert create_body["interactive_mode"] is False
    assert result.session_file and result.session_file.parent == manus.manus_data_home()
    records = [json.loads(line) for line in result.session_file.read_text().splitlines()]
    assert records[-1]["type"] == "playmaker_meta"
    assert records[-1]["credit_usage"] == 1


def test_long_prompt_is_attached_without_extra_role_text(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    prompt = "x" * 16_001
    captured = {}

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            captured.update(body)
            return {"ok": True, "task_id": "task", "task_url": "url"}
        if path.endswith("listMessages"):
            return {
                "ok": True,
                "messages": [
                    {
                        "id": "status",
                        "type": "status_update",
                        "status_update": {"agent_status": "stopped"},
                    }
                ],
                "has_more": False,
            }
        if path.endswith("task.detail"):
            return {"ok": True, "task": {"has_running_background_jobs": False}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    handler.dispatch(prompt, tmp_path)
    parts = captured["message"]["content"]
    assert parts[0] == {
        "type": "text",
        "text": "Your full instructions are in the attached prompt.md. Follow them exactly.",
    }
    assert parts[1]["filename"] == "prompt.md"
    assert parts[1]["file_data"].startswith("data:text/markdown;base64,")
    encoded_prompt = parts[1]["file_data"].partition(",")[2]
    assert base64.b64decode(encoded_prompt).decode() == prompt


@pytest.mark.parametrize(
    ("status", "expected_code", "prefix"),
    [("waiting", 0, "[manus waiting: input]"), ("error", 1, "failed")],
)
def test_waiting_and_error(configured, tmp_path, monkeypatch, status, expected_code, prefix):
    handler = manus.ManusHandler()
    messages = [
        {
            "id": "answer",
            "type": "assistant_message",
            "assistant_message": {"content": "last answer", "delivery_kind": "progress"},
        },
        {
            "id": "error",
            "type": "error_message",
            "error_message": {"content": "failed"},
        },
        {
            "id": "status",
            "type": "status_update",
            "status_update": {
                "agent_status": status,
                "status_detail": {
                    "waiting_for_event_type": "input",
                    "waiting_description": "choose",
                },
            },
        },
    ]
    paths = []

    def request(method, path, key, body=None, *, query=None):
        paths.append(path)
        if path.endswith("task.create"):
            return {"ok": True, "task_id": "task"}
        if path.endswith("listMessages"):
            return {"ok": True, "messages": messages, "has_more": False}
        if path.endswith("task.detail"):
            return {"ok": True, "task": {}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)
    assert result.exit_code == expected_code
    assert result.initial_output.startswith(prefix)
    assert "/v2/task.confirmAction" not in paths


def test_timeout_stops_task(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    clock = iter([0.0, 61.0, 62.0, 63.0])
    monkeypatch.setattr(manus.time, "monotonic", lambda: next(clock, 10000.0))
    monkeypatch.setattr(
        manus,
        "agent_setting",
        lambda agent, key, default=None: {"poll_seconds": 0, "max_wait_minutes": 1}.get(
            key, default
        ),
    )
    stopped = []

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return {"ok": True, "task_id": "task", "task_url": "https://example.invalid/task"}
        if path.endswith("listMessages"):
            return {"ok": True, "messages": [], "has_more": False}
        if path.endswith("task.stop"):
            stopped.append(body["task_id"])
            return {"ok": True}
        if path.endswith("task.detail"):
            return {"ok": True, "task": {}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)
    assert stopped == ["task"]
    assert result.exit_code == 1
    assert "stopped after 1 minutes" in result.initial_output


def test_timeout_stops_task_with_running_background_jobs(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    clock = iter([0.0, 61.0, 62.0])
    monkeypatch.setattr(manus.time, "monotonic", lambda: next(clock, 10000.0))
    monkeypatch.setattr(
        manus,
        "agent_setting",
        lambda agent, key, default=None: {"poll_seconds": 10, "max_wait_minutes": 1}.get(
            key, default
        ),
    )
    monkeypatch.setattr(
        manus.time,
        "sleep",
        lambda seconds: pytest.fail("slept after wall-clock cap"),
    )
    stopped = []

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return {"ok": True, "task_id": "task", "task_url": "task-url"}
        if path.endswith("listMessages"):
            return {
                "ok": True,
                "messages": [
                    {
                        "id": "status",
                        "type": "status_update",
                        "status_update": {"agent_status": "stopped"},
                    }
                ],
                "has_more": False,
            }
        if path.endswith("task.detail"):
            return {
                "ok": True,
                "task": {"has_running_background_jobs": True, "task_url": "task-url"},
            }
        if path.endswith("task.stop"):
            stopped.append(body["task_id"])
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)

    assert stopped == ["task"]
    assert result.exit_code == 1


def test_resume_timeout_uses_task_url_from_session(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    session_path = manus.manus_data_home() / "task.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps(
            {
                "type": "playmaker_meta",
                "task_url": "https://example.invalid/resumed-task",
            }
        )
        + "\n"
        + json.dumps({"type": "playmaker_meta", "task_url": None})
        + "\n",
        encoding="utf-8",
    )
    clock = iter([0.0, 61.0, 62.0])
    monkeypatch.setattr(manus.time, "monotonic", lambda: next(clock, 10000.0))
    monkeypatch.setattr(
        manus,
        "agent_setting",
        lambda agent, key, default=None: {"poll_seconds": 0, "max_wait_minutes": 1}.get(
            key, default
        ),
    )

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.sendMessage") or path.endswith("task.stop"):
            return {"ok": True}
        if path.endswith("listMessages"):
            return {"ok": True, "messages": [], "has_more": False}
        if path.endswith("task.detail"):
            return {"ok": True, "task": {}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.resume("follow up", tmp_path, "task")

    assert result.exit_code == 1
    assert "https://example.invalid/resumed-task" in result.initial_output


def test_attachment_download_does_not_send_api_key(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"attachment"

    def urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    output = handler._download_attachments(
        "result",
        [
            {
                "attachments": [
                    {
                        "filename": "report.txt",
                        "url": "https://files.example.invalid/presigned",
                    }
                ]
            }
        ],
        tmp_path,
        KEY,
    )

    headers = {name.lower(): value for name, value in requests[0].header_items()}
    assert "x-manus-api-key" not in headers
    assert "Attachments:" in output


def test_rate_limit_retries_and_never_exposes_key(configured, monkeypatch, caplog, capsys):
    handler = manus.ManusHandler()
    attempts = 0
    backoffs = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"ok": true}'

    def urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                KEY,
                {},
                io.BytesIO(b'{"error":{"code":"rate_limited","message":"wait"}}'),
            )
        return Response()

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(manus.time, "sleep", backoffs.append)
    assert handler._request("GET", "/test", KEY)["ok"] is True
    assert attempts == 2
    assert len(backoffs) == 1
    assert 1 <= backoffs[0] <= 1.25

    def broken(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, KEY, {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(manus.urllib.request, "urlopen", broken)
    with pytest.raises(RuntimeError) as caught:
        handler._request("POST", "/v2/task.create", KEY)
    assert KEY not in str(caught.value)
    captured = capsys.readouterr()
    assert KEY not in caplog.text + captured.out + captured.err


def test_rate_limit_envelope_retries(configured, monkeypatch):
    handler = manus.ManusHandler()
    payloads = iter(
        [
            b'{"ok":false,"error":{"code":"rate_limited","message":"wait"}}',
            b'{"ok":true,"request_id":"retried"}',
        ]
    )
    attempts = 0
    backoffs = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return next(payloads)

    def urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        return Response()

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(manus.time, "sleep", backoffs.append)

    assert handler._request("GET", "/test", KEY)["request_id"] == "retried"
    assert attempts == 2
    assert len(backoffs) == 1
    assert 1 <= backoffs[0] <= 1.25


def test_parse_copied_fixture_has_order_and_string_timestamps():
    turns = manus.parse_session_file(FIXTURES / "manus_events_lite.jsonl")
    assert [turn.role for turn in turns[:2]] == ["user", "assistant"]
    assert turns[0].timestamp is not None
    assert turns[0].timestamp.year == 2026
    assert turns[1].timestamp is not None
    assert turns[1].content.startswith("I’ll assess only the README")


def test_invalid_model_refuses_before_http(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    http_calls = []
    monkeypatch.setattr(handler, "_request", lambda *args, **kwargs: http_calls.append(args))
    with pytest.raises(ValueError, match="valid values"):
        handler.dispatch("prompt", tmp_path, model="invalid")
    assert http_calls == []


def test_missing_key_message(monkeypatch, tmp_path):
    monkeypatch.delenv("MANUS_API_KEY", raising=False)
    monkeypatch.setattr(manus, "_keychain_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="playmaker-manus-api"):
        manus.ManusHandler().dispatch("prompt", tmp_path)


@pytest.mark.parametrize("profile", ["lite", "standard"])
def test_acceptance_fixture_dispatch_reports_early_id_and_result(
    configured, tmp_path, monkeypatch, profile
):
    handler = manus.ManusHandler()
    created = _fixture("manus_create.json")
    events = _events(f"manus_events_{profile}.jsonl")
    detail = _fixture(f"manus_detail_{profile}.json")
    started = []
    detail_calls = []

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return created
        if path.endswith("listMessages"):
            assert started == [created["task_id"]]
            return {"ok": True, "messages": events, "has_more": False}
        if path.endswith("task.detail"):
            detail_calls.append(path)
            return detail
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("fixture prompt", tmp_path, on_session_started=started.append)
    expected = next(
        event["assistant_message"]["content"]
        for event in events
        if event.get("assistant_message", {}).get("delivery_kind") == "result"
    )

    assert result.agent_session_id == created["task_id"]
    assert result.initial_output == expected

    assert result.exit_code == 0
    assert started == [created["task_id"]]
    assert detail_calls
    assert detail["task"]["status"] == "stopped"


def test_acceptance_prompt_partition_is_exact():
    handler = manus.ManusHandler()
    assert handler._parts("short prompt", []) == [{"type": "text", "text": "short prompt"}]

    boundary = "p" * 16_000
    assert handler._parts(boundary, []) == [{"type": "text", "text": boundary}]
    prompt = "p" * 16_001
    parts = handler._parts(prompt, [])
    assert parts[0] == {
        "type": "text",
        "text": "Your full instructions are in the attached prompt.md. Follow them exactly.",
    }
    assert len(parts) == 2
    assert parts[1]["type"] == "file"
    assert parts[1]["filename"] == "prompt.md"
    assert base64.b64decode(parts[1]["file_data"].partition(",")[2]).decode() == prompt


@pytest.mark.parametrize(
    ("status", "expected_prefix", "expected_exit_code"),
    [("waiting", "[manus waiting: answer]", 0), ("error", "agent failed", 1)],
)
def test_acceptance_waiting_and_error_statuses(
    configured, tmp_path, monkeypatch, status, expected_prefix, expected_exit_code
):
    handler = manus.ManusHandler()
    requested_paths = []

    def request(method, path, key, body=None, *, query=None):
        requested_paths.append(path)
        if path.endswith("task.create"):
            return {"ok": True, "task_id": "task"}
        if path.endswith("listMessages"):
            return {
                "ok": True,
                "messages": [
                    {
                        "id": "error",
                        "type": "error_message",
                        "error_message": {"content": "agent failed"},
                    },
                    {
                        "id": "status",
                        "type": "status_update",
                        "status_update": {
                            "agent_status": status,
                            "status_detail": {
                                "waiting_for_event_type": "answer",
                                "waiting_description": "reply",
                            },
                        },
                    },
                ],
                "has_more": False,
            }
        if path.endswith("task.detail"):
            return {"ok": True, "task": {}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)

    assert result.initial_output.startswith(expected_prefix)
    assert result.initial_output == (
        "[manus waiting: answer] reply" if status == "waiting" else "agent failed"
    )
    assert result.exit_code == expected_exit_code
    assert "/v2/task.confirmAction" not in requested_paths


def test_acceptance_tiny_wall_clock_cap_stops_task(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(manus.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manus.time, "sleep", sleep)
    monkeypatch.setattr(
        manus,
        "agent_setting",
        lambda agent, key, default=None: {"poll_seconds": 10, "max_wait_minutes": 0.001}.get(
            key, default
        ),
    )
    stops = []

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return {"ok": True, "task_id": "task", "task_url": "task-url"}
        if path.endswith("listMessages"):
            return {"ok": True, "messages": [], "has_more": False}
        if path.endswith("task.stop"):
            stops.append(body["task_id"])
            return {"ok": True}
        if path.endswith("task.detail"):
            return {"ok": True, "task": {}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)

    assert stops == ["task"]
    assert result.exit_code == 1
    assert sleeps == [0.06]
    assert "stopped after 0.001 minutes: task-url" in result.initial_output


@pytest.mark.parametrize("profile", ["lite", "standard"])
def test_acceptance_fixture_parse_order_and_string_timestamp(profile):
    from datetime import UTC, datetime

    filename = f"manus_events_{profile}.jsonl"
    turns = manus.parse_session_file(FIXTURES / filename)
    events = [
        event
        for event in _events(filename)
        if event["type"] in {"user_message", "assistant_message", "error_message"}
    ]
    assert [turn.role for turn in turns[:2]] == ["user", "assistant"]
    for turn, event in zip(turns, events, strict=True):
        assert isinstance(event["timestamp"], str)
        value = int(event["timestamp"])
        seconds = value / 1000 if value > 10_000_000_000 else value
        assert turn.timestamp == datetime.fromtimestamp(seconds, tz=UTC)


@pytest.mark.parametrize("operation", ["dispatch", "resume"])
def test_acceptance_invalid_model_makes_no_http_request(
    configured, tmp_path, monkeypatch, operation
):
    handler = manus.ManusHandler()
    requests = []
    monkeypatch.setattr(handler, "_request", lambda *args, **kwargs: requests.append(args))

    with pytest.raises(ValueError, match="valid values"):
        if operation == "dispatch":
            handler.dispatch("prompt", tmp_path, model="not-a-manus-model")
        else:
            handler.resume("prompt", tmp_path, "task", model="not-a-manus-model")

    assert requests == []


@pytest.mark.parametrize("command", ["dispatch", "continue"])
def test_keyless_cli_preflight(monkeypatch, tmp_path, command):
    from typer.testing import CliRunner

    from playmaker import cli

    monkeypatch.delenv("MANUS_API_KEY", raising=False)
    monkeypatch.setattr(manus, "_keychain_api_key", lambda: None)
    monkeypatch.setattr(cli.state, "init_db", lambda: None)
    monkeypatch.setattr(cli.state, "get_session", lambda _: {"id": "s", "agent": "manus"})
    monkeypatch.setattr(cli.state, "thread_root", lambda _: "s")
    monkeypatch.setattr(cli.state, "thread_sessions", lambda _: [])
    result = CliRunner().invoke(
        cli.app, [command, "manus" if command == "dispatch" else "s", "--prompt", "p"]
    )
    assert result.exit_code == 2, result.output
    assert manus.NO_KEY_MESSAGE in result.output


class _JSONResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.payload).encode()


def _status(value, event_id="status"):
    return {"id": event_id, "type": "status_update", "status_update": {"agent_status": value}}


@pytest.mark.parametrize("failure", ["url", "500", "timeout", "fatal"])
def test_poll_http_failures(configured, tmp_path, monkeypatch, failure):
    calls = []
    sleeps = []
    now = [0.0]
    monkeypatch.setattr(manus.time, "monotonic", lambda: now[0])

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    polls = 0

    def urlopen(request, timeout):
        nonlocal polls
        endpoint = manus.urllib.parse.urlparse(request.full_url).path
        calls.append(endpoint)
        if endpoint.endswith("listMessages"):
            polls += 1
            if polls == 1:
                if failure == "url":
                    raise urllib.error.URLError(KEY)
                if failure == "timeout":
                    raise TimeoutError(KEY)
                raise urllib.error.HTTPError(
                    request.full_url, 400 if failure == "fatal" else 503, KEY, {}, None
                )
            return _JSONResponse({"messages": [_status("stopped")]})
        if endpoint.endswith("task.stop"):
            raise urllib.error.URLError(KEY)
        return _JSONResponse({"task": {"credit_usage": 2}})

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(manus.time, "sleep", sleep)
    result = manus.ManusHandler()._poll("task", tmp_path, KEY, "standard", "url")
    assert result.exit_code == (1 if failure == "fatal" else 0)
    assert KEY not in result.initial_output
    assert json.loads(result.session_file.read_text().splitlines()[-1])["type"] == "playmaker_meta"
    if failure == "fatal":
        assert calls.count("/v2/task.stop") > 1
    else:
        assert polls == 3
        assert sleeps and sleeps[0] >= 1


def test_timeout_stop_precedes_failed_detail(configured, tmp_path, monkeypatch):
    clock = iter([0, 1801, 1802])
    monkeypatch.setattr(manus.time, "monotonic", lambda: next(clock, 10000.0))
    calls = []

    def request(method, path, *args, **kwargs):
        calls.append(path)
        raise RuntimeError("unavailable")

    handler = manus.ManusHandler()
    monkeypatch.setattr(handler, "_request", request)
    result = handler._poll("task", tmp_path, KEY, "standard", None)
    assert calls == ["/v2/task.stop", "/v2/task.detail"]
    assert result.exit_code == 1
    assert "stopped after 30 minutes" in result.initial_output
    assert json.loads(result.session_file.read_text().splitlines()[-1])["type"] == "playmaker_meta"


def test_task_attachment_isolation_and_collision(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    task_ids = iter(["one", "two"])
    attachment = {"filename": "../report.txt", "url": "https://files.invalid/report"}
    event = {
        "type": "assistant_message",
        "assistant_message": {
            "content": "answer",
            "attachments": [attachment, attachment, attachment],
        },
    }

    def request(method, path, *args, **kwargs):
        if path.endswith("task.create"):
            return {"task_id": next(task_ids)}
        if path.endswith("listMessages"):
            return {"messages": [event, _status("stopped")]}
        return {"task": {"credit_usage": 1}}

    def urlopen(request, timeout):
        assert not request.has_header("X-manus-api-key")
        return _JSONResponse("data")

    monkeypatch.setattr(handler, "_request", request)
    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    for task_id in ("one", "two"):
        result = handler.dispatch("p", tmp_path)
        directory = manus.manus_data_home() / task_id / "files"
        assert sorted(p.name for p in directory.iterdir()) == [
            "report-2.txt",
            "report-3.txt",
            "report.txt",
        ]
        assert str(directory / "report.txt") in result.initial_output
        records = [json.loads(line) for line in result.session_file.read_text().splitlines()]
        assert records.count(event) == 1


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("budget", [0.01, 30])
def test_rate_limits_retry_until_deadline(configured, monkeypatch, envelope, budget):
    now = [0.0]
    attempts = []
    monkeypatch.setattr(manus.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manus.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(manus, "agent_setting", lambda *args: budget)

    def urlopen(request, timeout):
        attempts.append(timeout)
        if envelope:
            return _JSONResponse({"ok": False, "error": {"code": "rate_limited"}})
        raise urllib.error.HTTPError(request.full_url, 429, KEY, {}, None)

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    with pytest.raises((RuntimeError, TimeoutError)):
        manus.ManusHandler()._request("GET", "/test", KEY)
    assert len(attempts) == 1 if budget == 0.01 else len(attempts) > 6
    assert now[0] <= budget * 60
    assert all(timeout <= min(60, budget * 60) for timeout in attempts)


def test_nonempty_title_and_unexplained_error(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()

    def request(method, path, key, body=None, **kwargs):
        if path.endswith("task.create"):
            assert body["title"] == "Actual title"
            return {"task_id": "task"}
        if path.endswith("listMessages"):
            return {"messages": [_status("error")]}
        return {"task": {}}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("\n  \nActual title\nrest", tmp_path)
    assert result.exit_code == 1
    assert result.initial_output == "Manus reported an error (no message)"


def test_resume_without_session_skips_history_and_advances_cursor(
    configured, tmp_path, monkeypatch
):
    handler = manus.ManusHandler()
    sent = False
    polls = 0

    def request(method, path, key, body=None, *, query=None):
        nonlocal sent, polls
        if path.endswith("sendMessage"):
            sent = True
            return {}
        if path.endswith("listMessages"):
            if not sent:
                return {"messages": [_status("waiting", "old")]}
            polls += 1
            if polls == 1:
                assert query["start_event_id"] == "old"
                return {
                    "messages": [_status("waiting", "old"), _status("running", "new")],
                    "next_cursor": "cursor",
                }
            assert query["cursor"] == "cursor"
            return {"messages": [_status("error", "end")]}
        return {"task": {}}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.resume("next", tmp_path, "task")
    assert polls == 2
    assert result.initial_output == "Manus reported an error (no message)"


@pytest.mark.parametrize("failure", ["429", "envelope", "503", "url", "timeout"])
@pytest.mark.parametrize("recover", [True, False])
def test_poll_retries_past_six_until_deadline(configured, tmp_path, monkeypatch, failure, recover):
    now = [0.0]
    polls = []
    stops = []
    delays = []
    monkeypatch.setattr(manus.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        manus, "agent_setting", lambda a, k, d=None: 3 if k == "max_wait_minutes" else d
    )

    def sleep(delay):
        delays.append(delay)
        now[0] += delay

    def urlopen(request, timeout):
        if "listMessages" in request.full_url:
            polls.append(now[0])
            if recover and len(polls) > 7:
                return _JSONResponse({"messages": [_status("stopped")]})
            if failure == "envelope":
                return _JSONResponse({"ok": False, "error": {"code": "rate_limited"}})
            if failure == "url":
                raise urllib.error.URLError("offline")
            if failure == "timeout":
                raise TimeoutError("slow")
            raise urllib.error.HTTPError(request.full_url, int(failure), "busy", {}, None)
        if "task.stop" in request.full_url:
            stops.append(now[0])
            return _JSONResponse({"ok": True})
        return _JSONResponse({"task": {"status": "stopped"}})

    monkeypatch.setattr(manus.time, "sleep", sleep)
    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    result = manus.ManusHandler()._poll("task", tmp_path, KEY, "standard", None)
    assert len(polls) > 6
    assert max(delays) <= 30
    assert result.exit_code == (0 if recover else 1)
    assert stops == ([] if recover else [180])


def test_resume_existing_stopped_session_returns_new_result(configured, tmp_path, monkeypatch):
    path = manus.manus_data_home() / "task.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_status("stopped", "old")) + "\n")
    sent = False
    polls = 0
    details = 0

    def request(method, endpoint, key, body=None, *, query=None):
        nonlocal sent, polls, details
        if endpoint.endswith("sendMessage"):
            sent = True
            return {}
        if endpoint.endswith("listMessages"):
            if not sent:
                return {"messages": [_status("stopped", "old"), _status("stopped", "stale")]}
            polls += 1
            if polls == 1:
                assert query["start_event_id"] == "stale"
                return {"messages": [_status("stopped", "stale")]}
            return {
                "messages": [
                    {
                        "id": "answer",
                        "type": "assistant_message",
                        "assistant_message": {"content": "NEW result", "delivery_kind": "result"},
                    },
                    _status("stopped", "new"),
                ]
            }
        if endpoint.endswith("task.detail"):
            details += 1
            return {"task": {"status": "running" if details == 1 else "stopped"}}
        raise AssertionError(endpoint)

    handler = manus.ManusHandler()
    monkeypatch.setattr(handler, "_request", request)
    result = handler.resume("next", tmp_path, "task")
    assert result.initial_output == "NEW result"
    assert result.exit_code == 0
    assert details == 3


@pytest.mark.parametrize("endpoint", ["task.create", "task.sendMessage"])
@pytest.mark.parametrize("failure", ["timeout", "url", "503", "429", "envelope"])
def test_non_idempotent_posts_retry_only_rejected_requests(
    configured, monkeypatch, endpoint, failure
):
    calls = []
    monkeypatch.setattr(manus.time, "sleep", lambda _: None)

    def urlopen(request, timeout):
        calls.append(request)
        if len(calls) > 1:
            return _JSONResponse({"ok": True})
        if failure == "timeout":
            raise TimeoutError(KEY)
        if failure == "url":
            raise urllib.error.URLError(KEY)
        if failure == "envelope":
            return _JSONResponse({"ok": False, "error": {"code": "rate_limited"}})
        raise urllib.error.HTTPError(request.full_url, int(failure), KEY, {}, None)

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    handler = manus.ManusHandler()
    if failure in {"429", "envelope"}:
        assert handler._request("POST", "/v2/" + endpoint, KEY)["ok"]
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError):
            handler._request("POST", "/v2/" + endpoint, KEY)
        assert len(calls) == 1


def test_http_error_retains_redacted_server_diagnostics(configured, monkeypatch):
    def urlopen(request, timeout):
        body = json.dumps({"error": {"code": "invalid_profile", "message": "Bad profile " + KEY}})
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(body.encode()))

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="invalid_profile.*Bad profile") as caught:
        manus.ManusHandler()._request("GET", "/test", KEY)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("failure", ["value", "incomplete", "os", "url"])
def test_attachment_failures_are_reported_and_next_download_continues(
    configured, tmp_path, monkeypatch, failure
):
    from http.client import IncompleteRead

    errors = {
        "value": ValueError(),
        "incomplete": IncompleteRead(b"partial"),
        "os": OSError(),
        "url": urllib.error.URLError("offline"),
    }

    def urlopen(request, timeout):
        if request.full_url.endswith("bad"):
            raise errors[failure]
        return _JSONResponse("ok")

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    output = manus.ManusHandler()._download_attachments(
        "answer",
        [
            {
                "attachments": [
                    {"filename": "bad.txt", "url": "https://files.invalid/bad"},
                    {"filename": "good.txt", "url": "https://files.invalid/good"},
                ]
            }
        ],
        tmp_path,
        KEY,
    )
    assert "failed: bad.txt" in output
    assert str(tmp_path / "files" / "good.txt") in output


def test_timeout_final_detail_has_fresh_short_budget(configured, tmp_path, monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(manus.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manus.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if "listMessages" in request.full_url:
            now[0] = 1800
            raise TimeoutError()
        assert 0 < timeout <= 15
        if "task.detail" in request.full_url:
            return _JSONResponse({"task": {"credit_usage": "42", "agent_profile": "max"}})
        return _JSONResponse({"ok": True})

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    result = manus.ManusHandler()._poll("task", tmp_path, KEY, "standard", None)
    meta = json.loads(result.session_file.read_text().splitlines()[-1])
    assert result.exit_code == 1
    assert "task.stop" in calls[-2] and "task.detail" in calls[-1]
    assert meta["credit_usage"] == 42
    assert meta["agent_profile"] == "max"


@pytest.mark.parametrize("failure", ["IncompleteRead", "BadStatusLine", "LineTooLong", "json"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("recover", [False, True])
def test_protocol_and_non_json_failures_retry_only_gets(
    configured, monkeypatch, failure, method, recover
):
    import http.client

    now = [0.0]
    attempts = []
    monkeypatch.setattr(manus.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(manus.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    handler = manus.ManusHandler()
    handler._deadline = 90

    class Response(_JSONResponse):
        def read(self):
            if recover and len(attempts) > 2:
                return super().read()
            if failure == "json":
                return b"<html>temporary upstream error</html>"
            raise getattr(http.client, failure)(b"partial")

    def urlopen(request, timeout):
        attempts.append(timeout)
        return Response({"ok": True})

    monkeypatch.setattr(manus.urllib.request, "urlopen", urlopen)
    if method == "POST":
        with pytest.raises(RuntimeError):
            handler._request(method, "/v2/task.sendMessage", KEY)
        assert len(attempts) == 1
        assert now[0] == 0
    elif recover:
        assert handler._request(method, "/v2/task.listMessages", KEY)["ok"]
        assert len(attempts) == 3
        assert 0 < now[0] < 90
    else:
        with pytest.raises(TimeoutError):
            handler._request(method, "/v2/task.listMessages", KEY)
        assert len(attempts) > 6
        assert now[0] == 90


@pytest.mark.parametrize("value", ["  secret-that-must-not-leak \n", " \t\n", ""])
def test_environment_api_key_is_stripped(monkeypatch, value):
    monkeypatch.setenv("MANUS_API_KEY", value)
    monkeypatch.setattr(manus, "_keychain_api_key", lambda: None)
    if value.strip():
        assert manus.load_api_key() == KEY
    else:
        with pytest.raises(RuntimeError, match="manus: no API key"):
            manus.load_api_key()
        assert not manus.ManusHandler().is_available()
        monkeypatch.setattr(manus, "_keychain_api_key", lambda: "keychain-key")
        assert manus.load_api_key() == "keychain-key"


def test_cascade_self_wait_reaches_result_and_attachment(configured, tmp_path, monkeypatch):
    handler = manus.ManusHandler()
    events = _events("manus_events_cascade.jsonl")
    details = iter(
        [
            _fixture("manus_detail_cascade_running.json"),
            _fixture("manus_detail_cascade_final.json"),
            _fixture("manus_detail_cascade_final.json"),
        ]
    )
    message_pages = iter([events[:5], events[5:], [], []])

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return {"task_id": "iEYga7GeD8HEKJ7M38Jdm4"}
        if path.endswith("listMessages"):
            return {"messages": next(message_pages), "has_more": False}
        if path.endswith("task.detail"):
            return next(details)
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    monkeypatch.setattr(
        manus.urllib.request, "urlopen", lambda request, timeout: _JSONResponse("x")
    )
    result = handler.dispatch("prompt", tmp_path, model="max")

    assert result.exit_code == 0
    assert result.initial_output.startswith("[Download the reference]")
    assert result.initial_output.endswith("reference.md")
    meta = json.loads(result.session_file.read_text().splitlines()[-1])
    assert meta["credit_usage"] == 230


@pytest.mark.parametrize("event_type", ["messageAskUser", "gmailSendAction"])
def test_user_wait_returns_without_another_poll(configured, tmp_path, monkeypatch, event_type):
    handler = manus.ManusHandler()
    list_calls = 0

    def request(method, path, key, body=None, *, query=None):
        nonlocal list_calls
        if path.endswith("task.create"):
            return {"task_id": "task"}
        if path.endswith("listMessages"):
            list_calls += 1
            return {
                "messages": [
                    {
                        "type": "status_update",
                        "status_update": {
                            "agent_status": "waiting",
                            "status_detail": {
                                "waiting_for_event_type": event_type,
                                "waiting_description": "client input",
                            },
                        },
                    }
                ]
            }
        if path.endswith("task.detail"):
            return {"task": {}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)
    assert result.initial_output == f"[manus waiting: {event_type}] client input"
    assert list_calls == 1


@pytest.mark.parametrize("detail_status, expected_wait", [("waiting", True), ("running", False)])
def test_unknown_wait_checks_detail(
    configured, tmp_path, monkeypatch, detail_status, expected_wait
):
    handler = manus.ManusHandler()
    pages = iter(
        [
            [
                {
                    "id": "wait",
                    "type": "status_update",
                    "status_update": {
                        "agent_status": "waiting",
                        "status_detail": {"waiting_for_event_type": "futureWait"},
                    },
                }
            ],
            [_status("stopped", "stop")],
            [],
        ]
    )
    detail_calls = 0

    def request(method, path, key, body=None, *, query=None):
        nonlocal detail_calls
        if path.endswith("task.create"):
            return {"task_id": "task"}
        if path.endswith("listMessages"):
            return {"messages": next(pages), "has_more": False}
        if path.endswith("task.detail"):
            detail_calls += 1
            if detail_calls == 1:
                return {"task": {"status": detail_status}}
            return {"task": {"status": "stopped"}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)
    assert result.initial_output.startswith("[manus waiting:") is expected_wait


def test_configured_custom_self_wait(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(
        manus,
        "agent_setting",
        lambda agent, key, default=None: {
            "poll_seconds": 0,
            "max_wait_minutes": 30,
            "self_wait_types": ["customInternal"],
        }.get(key, default),
    )
    handler = manus.ManusHandler()
    pages = iter(
        [
            [
                {
                    "id": "wait",
                    "type": "status_update",
                    "status_update": {
                        "agent_status": "waiting",
                        "status_detail": {"waiting_for_event_type": "customInternal"},
                    },
                }
            ],
            [_status("stopped", "stop")],
            [],
        ]
    )

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return {"task_id": "task"}
        if path.endswith("listMessages"):
            return {"messages": next(pages), "has_more": False}
        if path.endswith("task.detail"):
            return {"task": {"status": "stopped"}}
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    assert not handler.dispatch("prompt", tmp_path).initial_output.startswith("[manus waiting:")


def test_stop_remote_already_stopped_skips_stop(configured, monkeypatch):
    handler = manus.ManusHandler()
    calls = []

    def request(method, path, key, body=None, *, query=None):
        calls.append((method, path, body, query))
        return {"task": {"status": "stopped", "has_running_background_jobs": False}}

    monkeypatch.setattr(handler, "_request", request)
    assert handler.stop_remote("task") == "stopped"
    assert [call[1] for call in calls] == ["/v2/task.detail"]


def test_stop_remote_running_stops_then_reads_status(configured, monkeypatch):
    handler = manus.ManusHandler()
    calls = []
    details = iter([{"task": {"status": "running"}}, {"task": {"status": "stopped"}}])

    def request(method, path, key, body=None, *, query=None):
        calls.append((method, path, body, query))
        if path.endswith("task.stop"):
            return {"ok": True}
        return next(details)

    monkeypatch.setattr(handler, "_request", request)
    assert handler.stop_remote("task") == "stopped"
    assert [call[1] for call in calls] == [
        "/v2/task.detail",
        "/v2/task.stop",
        "/v2/task.detail",
    ]


def test_stop_remote_stop_error_is_ignored_when_detail_is_stopped(configured, monkeypatch):
    handler = manus.ManusHandler()
    details = iter([{"task": {"status": "running"}}, {"task": {"status": "stopped"}}])

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.stop"):
            raise RuntimeError("stop response unavailable")
        return next(details)

    monkeypatch.setattr(handler, "_request", request)
    assert handler.stop_remote("task") == "stopped"


def test_unknown_wait_detail_is_not_reused_after_later_error(
    configured, tmp_path, monkeypatch
):
    handler = manus.ManusHandler()
    pages = iter(
        [
            [
                {
                    "id": "wait",
                    "type": "status_update",
                    "status_update": {
                        "agent_status": "waiting",
                        "status_detail": {"waiting_for_event_type": "futureWait"},
                    },
                }
            ],
            [
                {"id": "error-message", "type": "error_message", "error_message": "failed"},
                _status("error", "error-status"),
            ],
        ]
    )
    details = iter(
        [
            {"task": {"status": "running", "credit_usage": 40}},
            {"task": {"status": "error", "credit_usage": 41}},
        ]
    )

    def request(method, path, key, body=None, *, query=None):
        if path.endswith("task.create"):
            return {"task_id": "task"}
        if path.endswith("listMessages"):
            return {"messages": next(pages), "has_more": False}
        if path.endswith("task.detail"):
            return next(details)
        raise AssertionError(path)

    monkeypatch.setattr(handler, "_request", request)
    result = handler.dispatch("prompt", tmp_path)
    meta = json.loads(result.session_file.read_text().splitlines()[-1])
    assert result.exit_code == 1
    assert meta["credit_usage"] == 41

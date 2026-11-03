from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from playmaker.agents import perplexity

FIXTURES = Path(__file__).parent / "fixtures"


class Response:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body


def fixture(name: str) -> dict:
    data = json.loads((FIXTURES / f"perplexity_{name}.json").read_text())
    return data.get("body", data)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "secret-key")
    monkeypatch.setattr(perplexity.state, "PLAYMAKER_HOME", tmp_path / "state")
    monkeypatch.setattr(
        perplexity,
        "agent_setting",
        lambda _agent, setting, default=None: {"poll_seconds": 0}.get(setting, default),
    )


def test_dispatch_capture_and_sources(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    create = fixture("bg_create")
    final = fixture("bg_final")
    urlopen = Mock(side_effect=[Response(create), Response(final)])
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)
    started: list[str] = []

    result = perplexity.PerplexityHandler().dispatch(
        "prompt exactly", tmp_path, on_session_started=started.append
    )

    assert result.exit_code == 0
    assert result.agent_session_id == create["id"]
    assert started == [create["id"]]
    assert result.initial_output.startswith(perplexity._message_text(final) + "\n\nSources:")
    assert result.initial_output.endswith("14 results, 1 cited")


def test_completed_multi_sources_are_citation_order() -> None:
    response = fixture("completed_multi")
    output = perplexity.PerplexityHandler._completed_output(
        response, perplexity._message_text(response)
    )
    sources = output.split("\n\nSources:\n", 1)[1].rsplit("\n— perplexity", 1)[0]
    lines = sources.splitlines()
    assert lines[:2] == [
        "[web:101] sno-ai/sno-station: Turn the agents you already use into a ... "
        "— https://github.com/sno-ai/sno-station",
        "[web:112] README.md - Sno Station Skills "
        "— https://github.com/sno-ai/sno-station-skills/blob/main/README.md",
    ]
    assert len(lines) == 14
    assert not any(line.startswith("[web:1]") for line in lines)


def test_invalid_effort_and_files_refuse_before_http(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    urlopen = Mock()
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)
    handler = perplexity.PerplexityHandler()

    with pytest.raises(ValueError, match="invalid model"):
        handler.dispatch("x", tmp_path, model="turbo")
    with pytest.raises(ValueError, match="--files is not supported"):
        handler.dispatch("x", tmp_path, files=[tmp_path / "x"])
    assert not urlopen.called
    assert handler._effort("low@2026-10-24.0") == "low@2026-10-24.0"


def test_missing_instructions_refuses_before_http(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        perplexity,
        "agent_setting",
        lambda _agent, setting, default=None: (
            str(tmp_path / "missing") if setting == "instructions_file" else default
        ),
    )
    urlopen = Mock()
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)

    with pytest.raises(ValueError, match="cannot read instructions_file"):
        perplexity.PerplexityHandler().dispatch("x", tmp_path)
    assert not urlopen.called


def test_instructions_and_input_are_sent_exactly(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2031, 4, 5, 12, tzinfo=UTC)

    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Date=${current_date}\nKeep ${other} verbatim.\n", encoding="utf-8")
    monkeypatch.setattr(perplexity, "datetime", FakeDatetime)
    monkeypatch.setattr(
        perplexity,
        "agent_setting",
        lambda _agent, setting, default=None: {
            "instructions_file": str(instructions),
            "poll_seconds": 0,
        }.get(setting, default),
    )
    urlopen = Mock(
        side_effect=[Response(fixture("bg_create")), Response(fixture("bg_final"))]
    )
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)
    prompt = "first byte\n\x00last byte"

    perplexity.PerplexityHandler().dispatch(prompt, tmp_path)

    body = json.loads(urlopen.call_args_list[0].args[0].data)
    assert body["input"] == prompt
    assert body["instructions"] == "Date=2031-04-05\nKeep ${other} verbatim.\n"

    monkeypatch.setattr(
        perplexity,
        "agent_setting",
        lambda _agent, setting, default=None: {"poll_seconds": 0}.get(setting, default),
    )
    urlopen.reset_mock(side_effect=True)
    urlopen.side_effect = [Response(fixture("bg_create")), Response(fixture("bg_final"))]
    perplexity.PerplexityHandler().dispatch(prompt, tmp_path)
    body = json.loads(urlopen.call_args_list[0].args[0].data)
    assert body["input"] == prompt
    assert "instructions" not in body


def test_wall_clock_cap_posts_cancel(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(perplexity.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(perplexity.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        perplexity,
        "agent_setting",
        lambda _agent, setting, default=None: {
            "max_wait_minutes": 1 / 60,
            "poll_seconds": 3,
        }.get(setting, default),
    )
    queued = dict(fixture("bg_create"))
    urlopen = Mock(
        side_effect=[
            Response(fixture("bg_create")),
            Response(queued),
            Response(fixture("cancel_post")),
        ]
    )
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)

    result = perplexity.PerplexityHandler().dispatch("wait", tmp_path)

    assert result.exit_code == 1
    assert "cancelled after" in result.initial_output
    cancel_request = urlopen.call_args_list[-1].args[0]
    assert cancel_request.method == "POST"
    assert cancel_request.full_url.endswith(f"/{queued['id']}/cancel")


def test_wall_clock_cap_writes_session_meta(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(perplexity.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(perplexity.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        perplexity,
        "agent_setting",
        lambda _agent, setting, default=None: {
            "max_wait_minutes": 1 / 60,
            "poll_seconds": 3,
        }.get(setting, default),
    )
    create = fixture("bg_create")
    urlopen = Mock(
        side_effect=[
            Response(create),
            Response(create),
            Response(fixture("cancel_post")),
        ]
    )
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)

    handler = perplexity.PerplexityHandler()
    result = handler.dispatch("wait", tmp_path)

    assert result.exit_code == 1
    assert result.session_file is not None
    assert result.session_file.is_file()
    assert handler.find_session_file(create["id"], tmp_path) == result.session_file
    (meta,) = [json.loads(line) for line in result.session_file.read_text().splitlines()]
    assert meta["type"] == "playmaker_meta"
    assert meta["response_id"] == create["id"]
    assert meta["cost_usd"] is None
    assert meta["input"] == "wait"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("failed", "backend failed"),
        ("cancelled", "perplexity: cancelled"),
        ("incomplete", "partial answer\n[perplexity incomplete: max_output_tokens]"),
    ],
)
def test_terminal_status_mapping(
    configured,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    expected: str,
) -> None:
    terminal = dict(fixture("bg_final"))
    terminal["status"] = status
    if status == "failed":
        terminal["error"] = {"message": "backend failed"}
        terminal["output"] = []
    elif status == "incomplete":
        terminal["incomplete_details"] = {"reason": "max_output_tokens"}
        terminal["output"] = [
            {"type": "message", "content": [{"type": "output_text", "text": "partial answer"}]}
        ]
    urlopen = Mock(side_effect=[Response(fixture("bg_create")), Response(terminal)])
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)

    result = perplexity.PerplexityHandler().dispatch("status", tmp_path)

    assert result.exit_code == 1
    assert result.initial_output == expected


def test_create_429_retry_and_api_key_redaction(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    rate_limit = HTTPError(
        perplexity.BASE_URL,
        429,
        "rate limited",
        {"Retry-After": "0"},
        BytesIO(
            json.dumps(
                {
                    "error": {
                        "message": "Request rate limit exceeded",
                        "type": "request_rate_limit_exceeded",
                        "code": 429,
                    }
                }
            ).encode()
        ),
    )
    urlopen = Mock(
        side_effect=[rate_limit, Response(fixture("bg_create")), Response(fixture("bg_final"))]
    )
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)

    result = perplexity.PerplexityHandler().dispatch("retry", tmp_path)

    create_calls = [
        call.args[0] for call in urlopen.call_args_list if call.args[0].method == "POST"
    ]
    assert result.exit_code == 0
    assert len(create_calls) == 2
    assert {json.loads(request.data)["input"] for request in create_calls} == {"retry"}

    leaked = HTTPError(
        perplexity.BASE_URL,
        400,
        "bad request",
        {},
        BytesIO(json.dumps({"error": {"message": "secret-key is invalid"}}).encode()),
    )
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", Mock(side_effect=leaked))
    redacted = perplexity.PerplexityHandler().dispatch("redact", tmp_path)
    assert "secret-key" not in redacted.initial_output
    assert "secret-key" not in repr(redacted)
    assert "secret-key" not in caplog.text


def test_resume_uses_last_completed_id_and_appends_session(
    configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    urlopen = Mock(
        side_effect=[
            Response(fixture("bg_create")),
            Response(fixture("bg_final")),
            Response(fixture("followup_create")),
            Response(fixture("followup_final")),
        ]
    )
    monkeypatch.setattr(perplexity.urllib.request, "urlopen", urlopen)
    handler = perplexity.PerplexityHandler()
    first = handler.dispatch("first", tmp_path)
    original_lines = first.session_file.read_text(encoding="utf-8").splitlines()
    started: list[str] = []

    resumed = handler.resume(
        "follow up",
        tmp_path,
        first.agent_session_id,
        on_session_started=started.append,
    )

    resume_body = json.loads(urlopen.call_args_list[2].args[0].data)
    assert resume_body["previous_response_id"] == fixture("bg_final")["id"]
    assert resumed.agent_session_id == first.agent_session_id
    assert resumed.session_file == first.session_file
    assert started == [first.agent_session_id]
    appended = resumed.session_file.read_text(encoding="utf-8").splitlines()
    assert appended[:2] == original_lines
    assert len(appended) == 4


def test_parse_session_file(configured, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        perplexity.urllib.request,
        "urlopen",
        Mock(side_effect=[Response(fixture("bg_create")), Response(fixture("bg_final"))]),
    )
    result = perplexity.PerplexityHandler().dispatch("question", tmp_path)

    turns = perplexity.parse_session_file(result.session_file)
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "question"),
        ("assistant", perplexity._message_text(fixture("bg_final"))),
    ]

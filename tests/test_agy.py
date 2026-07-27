from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playmaker.agents.agy import AgyHandler


def _write_transcript(path: Path, steps: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(s) for s in steps), encoding="utf-8")


def test_parse_session_file_maps_roles(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript_full.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-07-12T13:26:29Z",
                "content": "<USER_REQUEST>\nDo the thing\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\nnoise\n</ADDITIONAL_METADATA>",
            },
            {"step_index": 1, "source": "SYSTEM", "type": "CONVERSATION_HISTORY"},
            # streaming placeholder — must be dropped
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": ""},
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "RUN_COMMAND",
                "created_at": "2026-07-12T13:26:31Z",
                "content": "The command completed successfully.",
            },
            {"step_index": 4, "source": "SYSTEM", "type": "CHECKPOINT", "content": "{{ CHECKPOINT }}"},
            {
                "step_index": 5,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-07-12T13:26:33Z",
                "content": "DONE",
            },
        ],
    )

    turns = AgyHandler().parse_session_file(transcript)

    assert [t.role for t in turns] == ["user", "tool", "assistant"]
    assert turns[0].content == "Do the thing"
    assert turns[1].tool_calls[0]["name"] == "RUN_COMMAND"
    assert turns[1].tool_results[0]["content"] == "The command completed successfully."
    assert turns[2].content == "DONE"
    assert turns[2].timestamp is not None


def test_parse_session_file_missing_file() -> None:
    assert AgyHandler().parse_session_file(Path("/nonexistent/transcript.jsonl")) == []


def test_build_prompt_prepends_workspace_preamble(tmp_path: Path) -> None:
    prompt = AgyHandler._build_prompt("Fix the bug", [tmp_path / "a.py"], tmp_path)

    assert prompt.startswith(f"Workspace root: {tmp_path}")
    assert "absolute paths" in prompt
    assert "Fix the bug" in prompt
    assert str(tmp_path / "a.py") in prompt


def test_validate_model_rejects_unknown(monkeypatch) -> None:
    import pytest

    handler = AgyHandler()
    monkeypatch.setattr(
        AgyHandler,
        "available_models",
        staticmethod(lambda: ("Gemini 3.5 Flash (Low)", "Claude Opus 4.6 (Thinking)")),
    )

    with pytest.raises(RuntimeError, match="agy has no model"):
        handler._validate_model("No Such Model 9000")


def test_validate_model_accepts_known(monkeypatch) -> None:
    handler = AgyHandler()
    monkeypatch.setattr(
        AgyHandler,
        "available_models",
        staticmethod(lambda: ("Gemini 3.5 Flash (Low)",)),
    )

    # Exact match and None both pass without raising.
    handler._validate_model("Gemini 3.5 Flash (Low)")
    handler._validate_model(None)


def test_validate_model_skips_when_roster_unavailable(monkeypatch) -> None:
    handler = AgyHandler()
    monkeypatch.setattr(AgyHandler, "available_models", staticmethod(lambda: ()))

    # Empty roster (probe failed) → don't block dispatch on a probe hiccup.
    handler._validate_model("Anything At All")


def test_find_session_file_prefers_full_transcript(tmp_path: Path, monkeypatch) -> None:
    import playmaker.agents.agy as agy_mod

    monkeypatch.setattr(agy_mod, "AGY_BRAIN_ROOT", tmp_path)
    conv = "0" * 36
    logs = tmp_path / conv / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text("", encoding="utf-8")
    (logs / "transcript_full.jsonl").write_text("", encoding="utf-8")

    found = AgyHandler().find_session_file(conv, tmp_path)

    assert found == logs / "transcript_full.jsonl"

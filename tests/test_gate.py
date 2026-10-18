"""Acceptance gates are recorded separately from the agent outcome."""

from __future__ import annotations

from pathlib import Path

from playmaker import gate


def test_gate_pass_fail_and_timeout_write_logs(tmp_path: Path) -> None:
    passed = gate.run("echo ok", tmp_path, tmp_path / "pass.log", 1)
    failed = gate.run("echo bad; exit 7", tmp_path, tmp_path / "fail.log", 1)
    timed_out = gate.run("sleep 1", tmp_path, tmp_path / "timeout.log", 0.01)

    assert (passed.status, passed.exit_code) == ("pass", 0)
    assert (failed.status, failed.exit_code) == ("fail", 7)
    assert (timed_out.status, timed_out.exit_code) == ("timeout", 124)
    assert "ok" in passed.log_path.read_text()
    assert "bad" in failed.log_path.read_text()

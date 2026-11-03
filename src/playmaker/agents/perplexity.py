"""Perplexity Agent API lane."""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playmaker import state
from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import agent_setting

BASE_URL = "https://api.perplexity.ai/v1/agent"
KEYCHAIN_SERVICE = "playmaker-perplexity-api"
KEYCHAIN_ACCOUNT = "playmaker"
VALID_EFFORTS = ("fast", "low", "medium", "high", "xhigh", "ultra-preview")
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "incomplete"}
NO_KEY_MESSAGE = (
    "perplexity: no API key — add one: security add-generic-password "
    "-s playmaker-perplexity-api -a playmaker -w  (or set PERPLEXITY_API_KEY)"
)


def _keychain_api_key() -> str | None:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def load_api_key() -> str:
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip() or _keychain_api_key()
    if not key:
        raise RuntimeError(NO_KEY_MESSAGE)
    return key


def perplexity_data_home() -> Path:
    return state.PLAYMAKER_HOME / "perplexity"


def _timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _message_text(response: dict[str, Any]) -> str:
    for item in reversed(response.get("output", [])):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        texts = [
            part.get("text", "")
            for part in item.get("content", [])
            if isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ]
        if texts:
            return "".join(texts)
    return ""


def parse_session_file(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError):
        return turns
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("type") != "playmaker_meta":
            continue
        response = records[index - 1] if index and isinstance(records[index - 1], dict) else {}
        turns.append(
            Turn(
                role="user",
                content=str(record.get("input", "")),
                timestamp=_timestamp(response.get("created_at")),
            )
        )
        text = _message_text(response)
        if text:
            turns.append(
                Turn(
                    role="assistant",
                    content=text,
                    timestamp=_timestamp(response.get("completed_at")),
                )
            )
    return turns


class PerplexityHandler:
    name = "perplexity"

    def preflight(self) -> None:
        load_api_key()

    def is_available(self) -> bool:
        try:
            load_api_key()
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _effort(model: str | None) -> str:
        value = str(model or agent_setting("perplexity", "model", "low"))
        name, separator, version = value.partition("@")
        if name not in VALID_EFFORTS or (separator and (not version or "@" in version)):
            raise ValueError(
                f"perplexity: invalid model {value!r}; valid values: "
                + ", ".join(VALID_EFFORTS)
                + " (each optionally @<version>)"
            )
        return value

    @staticmethod
    def _instructions() -> tuple[str | None, str | None]:
        configured = agent_setting("perplexity", "instructions_file")
        if not configured:
            return None, None
        path = Path(str(configured)).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"perplexity: cannot read instructions_file {path}: {exc}") from None
        if not text:
            raise ValueError(f"perplexity: instructions_file is empty: {path}")
        today = datetime.now(UTC).date().isoformat()
        return text.replace("${current_date}", today), str(path)

    @staticmethod
    def _validate_files(files: list[Path] | None) -> None:
        if files:
            raise ValueError(
                "perplexity: --files is not supported; paste the text into the prompt"
            )

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        self._validate_files(files)
        effort = self._effort(model)
        instructions, instructions_file = self._instructions()
        key = load_api_key()
        started = time.monotonic()
        deadline = started + float(agent_setting("perplexity", "max_wait_minutes", 20)) * 60
        body: dict[str, Any] = {"input": prompt, "effort": effort, "background": True}
        if instructions is not None:
            body["instructions"] = instructions
        response_id = ""
        try:
            created = self._request("POST", "", key, body, deadline, retry_create=True)
            response_id = self._response_id(created)
            self._notify(on_session_started, response_id)
            return self._poll(
                response_id,
                response_id,
                prompt,
                cwd,
                key,
                effort,
                instructions_file,
                started,
                deadline,
            )
        except (RuntimeError, TimeoutError) as exc:
            return self._error_result(
                response_id, cwd, started, str(exc).replace(key, "[redacted]")
            )

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        self._validate_files(files)
        effort = self._effort(model)
        instructions, instructions_file = self._instructions()
        path = self.find_session_file(agent_session_id, cwd)
        if path is None:
            raise ValueError(f"perplexity: unknown session {agent_session_id!r}")
        previous = self._last_completed_id(path)
        if previous is None:
            raise ValueError(f"perplexity: session {agent_session_id!r} has no completed response")
        key = load_api_key()
        started = time.monotonic()
        deadline = started + float(agent_setting("perplexity", "max_wait_minutes", 20)) * 60
        body: dict[str, Any] = {
            "input": prompt,
            "effort": effort,
            "background": True,
            "previous_response_id": previous,
        }
        if instructions is not None:
            body["instructions"] = instructions
        try:
            created = self._request("POST", "", key, body, deadline, retry_create=True)
            response_id = self._response_id(created)
            self._notify(on_session_started, agent_session_id)
            return self._poll(
                response_id,
                agent_session_id,
                prompt,
                cwd,
                key,
                effort,
                instructions_file,
                started,
                deadline,
            )
        except (RuntimeError, TimeoutError) as exc:
            return self._error_result(
                agent_session_id, cwd, started, str(exc).replace(key, "[redacted]"), path
            )

    @staticmethod
    def _response_id(response: dict[str, Any]) -> str:
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise RuntimeError("perplexity: create response missing id")
        return response_id

    @staticmethod
    def _notify(callback: SessionStartedCallback | None, response_id: str) -> None:
        if callback is not None:
            try:
                callback(response_id)
            except Exception:
                pass

    def _poll(
        self,
        response_id: str,
        first_id: str,
        prompt: str,
        cwd: Path,
        key: str,
        effort: str,
        instructions_file: str | None,
        started: float,
        deadline: float,
    ) -> DispatchResult:
        poll_seconds = float(agent_setting("perplexity", "poll_seconds", 3))
        while True:
            if time.monotonic() >= deadline:
                self._cancel(response_id, key)
                minutes = float(agent_setting("perplexity", "max_wait_minutes", 20))
                path = self._record_failed_run(
                    first_id, response_id, prompt, effort, instructions_file
                )
                return self._error_result(
                    first_id,
                    cwd,
                    started,
                    f"perplexity: run was cancelled after {minutes:g} minutes",
                    path,
                )
            try:
                response = self._request("GET", f"/{response_id}", key, None, deadline)
            except TimeoutError:
                self._cancel(response_id, key)
                minutes = float(agent_setting("perplexity", "max_wait_minutes", 20))
                path = self._record_failed_run(
                    first_id, response_id, prompt, effort, instructions_file
                )
                return self._error_result(
                    first_id,
                    cwd,
                    started,
                    f"perplexity: run was cancelled after {minutes:g} minutes",
                    path,
                )
            except RuntimeError as exc:
                self._cancel(response_id, key)
                path = self._record_failed_run(
                    first_id, response_id, prompt, effort, instructions_file
                )
                return self._error_result(
                    first_id,
                    cwd,
                    started,
                    str(exc).replace(key, "[redacted]"),
                    path,
                )
            status = response.get("status")
            if status in TERMINAL_STATUSES:
                return self._finish(
                    response, first_id, prompt, cwd, effort, instructions_file, started
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            time.sleep(min(poll_seconds, remaining))

    @staticmethod
    def _record_failed_run(
        first_id: str,
        response_id: str,
        prompt: str,
        effort: str,
        instructions_file: str | None,
    ) -> Path:
        path = perplexity_data_home() / f"{first_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "type": "playmaker_meta",
            "response_id": response_id,
            "effort": effort,
            "model": None,
            "cost_usd": None,
            "instructions_file": instructions_file,
            "input": prompt,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(meta, ensure_ascii=False) + "\n")
        return path

    def _cancel(self, response_id: str, key: str) -> None:
        try:
            self._request(
                "POST",
                f"/{response_id}/cancel",
                key,
                {},
                time.monotonic() + 15,
                retry_cancel=True,
            )
        except (RuntimeError, TimeoutError):
            pass

    def _finish(
        self,
        response: dict[str, Any],
        first_id: str,
        prompt: str,
        cwd: Path,
        requested_effort: str,
        instructions_file: str | None,
        started: float,
    ) -> DispatchResult:
        status = response.get("status")
        text = _message_text(response)
        if status == "completed":
            output, exit_code = self._completed_output(response, text), 0
        elif status == "cancelled":
            output, exit_code = "perplexity: cancelled", 1
        elif status == "incomplete":
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            suffix = f"[perplexity incomplete: {reason or 'unknown'}]"
            output, exit_code = (text + "\n" if text else "") + suffix, 1
        else:
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            error_type = error.get("type") if isinstance(error, dict) else None
            raw = str(message or "perplexity: failed")
            output = f"perplexity: quota unavailable: {raw}" if self._quota_error(
                f"{error_type or ''} {raw}"
            ) else raw
            exit_code = 1
        cost = self._cost(response)
        actual_effort = response.get("preset") or response.get("effort") or requested_effort
        meta = {
            "type": "playmaker_meta",
            "response_id": response.get("id"),
            "effort": actual_effort,
            "model": response.get("model"),
            "cost_usd": cost,
            "instructions_file": instructions_file,
            "input": prompt,
        }
        path = perplexity_data_home() / f"{first_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            stream.write(json.dumps(meta, ensure_ascii=False) + "\n")
        return DispatchResult(
            agent_session_id=first_id,
            cwd=str(cwd),
            session_file=path,
            initial_output=output,
            cost_usd=cost,
            duration_seconds=time.monotonic() - started,
            exit_code=exit_code,
        )

    @staticmethod
    def _cost(response: dict[str, Any]) -> float | None:
        usage = response.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        value = cost.get("total_cost") if isinstance(cost, dict) else None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _completed_output(response: dict[str, Any], text: str) -> str:
        results: dict[int, tuple[str, str]] = {}
        result_count = 0
        for item in response.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "search_results":
                continue
            for result in item.get("results", []):
                if not isinstance(result, dict):
                    continue
                result_count += 1
                result_id = result.get("id")
                if isinstance(result_id, int):
                    results[result_id] = (str(result.get("title", "")), str(result.get("url", "")))
        cited: list[int] = []
        for match in re.finditer(r"\[web:(\d+)\]", text):
            result_id = int(match.group(1))
            if result_id not in cited:
                cited.append(result_id)
        output = text
        if cited:
            lines = []
            for result_id in cited:
                if result_id in results:
                    title, url = results[result_id]
                    lines.append(f"[web:{result_id}] {title} — {url}")
                else:
                    lines.append(f"[web:{result_id}] (not in results)")
            output += "\n\nSources:\n" + "\n".join(lines)
        completed_at = _timestamp(response.get("completed_at"))
        date = completed_at.date().isoformat() if completed_at else "unknown"
        preset = response.get("preset") or response.get("effort") or "unknown"
        model = response.get("model") or "unknown"
        provenance = (
            f"— perplexity {preset} · {model} · {date} · "
            f"{result_count} results, {len(cited)} cited"
        )
        return output + ("\n" if output else "") + provenance

    def _request(
        self,
        method: str,
        path: str,
        key: str,
        body: dict[str, Any] | None,
        deadline: float,
        *,
        retry_create: bool = False,
        retry_cancel: bool = False,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None and method != "GET" else None
        request = urllib.request.Request(
            BASE_URL + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        delay = 1.0
        cancel_retries = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("perplexity: request deadline exceeded")
            retry_after: float | None = None
            try:
                with urllib.request.urlopen(request, timeout=min(60, remaining)) as response:
                    payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise RuntimeError("perplexity: API returned a non-object response")
                return payload
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read())
                except (ValueError, OSError, http.client.HTTPException):
                    payload = {}
                error = payload.get("error") if isinstance(payload, dict) else None
                message = str(
                    error.get("message", "request failed")
                    if isinstance(error, dict)
                    else "request failed"
                )
                error_type = str(error.get("type", "") if isinstance(error, dict) else "")
                if exc.code in {401, 403}:
                    raise RuntimeError("perplexity: API key was rejected") from None
                if exc.code == 402 or self._quota_error(error_type + " " + message):
                    raise RuntimeError(f"perplexity: quota unavailable: {message}") from None
                retry = exc.code == 429 and (retry_create or method == "GET" or retry_cancel)
                retry = retry or (method == "GET" and 500 <= exc.code < 600)
                if retry_cancel:
                    cancel_retries += 1
                    retry = retry and cancel_retries <= 3
                if not retry:
                    raise RuntimeError(
                        f"perplexity API error ({exc.code}): {message}".replace(key, "[redacted]")
                    ) from None
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = float(header) if header is not None else None
                except ValueError:
                    retry_after = None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                if method != "GET":
                    raise RuntimeError(
                        "perplexity: API request failed; acceptance unknown"
                    ) from None
            except (ValueError, UnicodeDecodeError):
                if method != "GET":
                    raise RuntimeError("perplexity: API returned invalid JSON") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("perplexity: request deadline exceeded")
            wait = retry_after if retry_after is not None else delay + random.uniform(0, delay / 4)
            time.sleep(min(wait, remaining))
            delay = min(delay * 2, 30)

    @staticmethod
    def _quota_error(message: str) -> bool:
        value = message.lower()
        return any(word in value for word in ("credit", "balance", "insufficient funds"))

    @staticmethod
    def _last_completed_id(path: Path) -> str | None:
        try:
            records = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for raw in reversed(records):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("status") == "completed":
                response_id = record.get("id")
                if isinstance(response_id, str):
                    return response_id
        return None

    @staticmethod
    def _error_result(
        session_id: str,
        cwd: Path,
        started: float,
        output: str,
        path: Path | None = None,
    ) -> DispatchResult:
        return DispatchResult(
            agent_session_id=session_id,
            cwd=str(cwd),
            session_file=path,
            initial_output=output,
            duration_seconds=time.monotonic() - started,
            exit_code=1,
        )

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        path = perplexity_data_home() / f"{agent_session_id}.jsonl"
        return path if path.is_file() else None

    def parse_session_file(self, path: Path) -> list[Turn]:
        return parse_session_file(path)

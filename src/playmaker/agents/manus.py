"""Manus API v2 agent lane."""

from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playmaker import state
from playmaker.agents.base import DispatchResult, SessionStartedCallback, Turn
from playmaker.config import agent_setting

BASE_URL = "https://api.manus.ai"
KEYCHAIN_SERVICE = "playmaker-manus-api"
KEYCHAIN_ACCOUNT = "playmaker"
MAX_FILE_BYTES = 20 * 1024 * 1024
VALID_MODELS = ("lite", "standard", "max", "max-medium", "max-high", "max-extra", "max-ultra")
NO_KEY_MESSAGE = (
    "manus: no API key — add one: security add-generic-password "
    "-s playmaker-manus-api -a playmaker -w  (or set MANUS_API_KEY)"
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
    key = os.environ.get("MANUS_API_KEY", "").strip() or _keychain_api_key()
    if not key:
        raise RuntimeError(NO_KEY_MESSAGE)
    return key


def manus_data_home() -> Path:
    return state.PLAYMAKER_HOME / "manus"


def _timestamp(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    # API timestamps are milliseconds; tolerate seconds as well.
    if value > 10_000_000_000:
        value /= 1000
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def parse_session_file(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return turns
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        role: str | None = None
        payload: object = None
        if kind == "user_message":
            role, payload = "user", event.get("user_message")
        elif kind == "assistant_message":
            role, payload = "assistant", event.get("assistant_message")
        elif kind == "error_message":
            role, payload = "system", event.get("error_message")
        if role is None:
            continue
        if isinstance(payload, dict):
            content = payload.get("content")
            text = content if isinstance(content, str) else ""
            attachments = payload.get("attachments")
            if isinstance(attachments, list):
                names = [a.get("filename") for a in attachments if isinstance(a, dict)]
                names = [name for name in names if isinstance(name, str) and name]
                if names:
                    text += ("\n" if text else "") + "Attachments: " + ", ".join(names)
        else:
            text = payload if isinstance(payload, str) else ""
        turns.append(Turn(role=role, content=text, timestamp=_timestamp(event)))
    return turns


class ManusHandler:
    name = "manus"

    def preflight(self) -> None:
        load_api_key()

    def is_available(self) -> bool:
        try:
            load_api_key()
        except RuntimeError:
            return False
        return True

    def dispatch(
        self,
        prompt: str,
        cwd: Path,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        profile = self._profile(model)
        key = load_api_key()
        body: dict[str, Any] = {
            "message": {"content": self._parts(prompt, files or [])},
            "agent_profile": profile,
            "interactive_mode": False,
            "title": next((line[:80] for line in prompt.splitlines() if line.strip()), ""),
        }
        project_id = agent_setting("manus", "project_id")
        if project_id:
            body["project_id"] = str(project_id)
        created = self._request("POST", "/v2/task.create", key, body)
        task_id = created.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("manus: task.create response missing task_id")
        self._notify(on_session_started, task_id)
        return self._poll(task_id, cwd, key, profile, created.get("task_url"))

    def resume(
        self,
        prompt: str,
        cwd: Path,
        agent_session_id: str,
        files: list[Path] | None = None,
        on_session_started: SessionStartedCallback | None = None,
        model: str | None = None,
    ) -> DispatchResult:
        profile = self._profile(model)
        key = load_api_key()
        path = manus_data_home() / f"{agent_session_id}.jsonl"
        seen = self._seen_ids(path)
        # Snapshot history before sending so old terminal events cannot end this run.
        cursor = None
        path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            query = {"task_id": agent_session_id, "order": "asc", "limit": 100}
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", "/v2/task.listMessages", key, query=query)
            with path.open("a", encoding="utf-8") as stream:
                for event in page.get("messages", []):
                    if isinstance(event, dict) and self._event_key(event) not in seen:
                        seen.add(self._event_key(event))
                        stream.write(json.dumps(event) + "\n")
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        self._request(
            "POST",
            "/v2/task.sendMessage",
            key,
            {
                "task_id": agent_session_id,
                "message": {"content": self._parts(prompt, files or [])},
                "agent_profile": profile,
            },
        )
        self._notify(on_session_started, agent_session_id)
        return self._poll(agent_session_id, cwd, key, profile, None)

    @staticmethod
    def _notify(callback: SessionStartedCallback | None, task_id: str) -> None:
        if callback is not None:
            try:
                callback(task_id)
            except Exception:
                pass

    @staticmethod
    def _profile(model: str | None) -> str:
        value = str(model or agent_setting("manus", "model", "standard"))
        if value.startswith("manus-"):
            return value
        if value not in VALID_MODELS:
            raise ValueError(
                f"manus: invalid model {value!r}; valid values: {', '.join(VALID_MODELS)}"
            )
        return value

    @staticmethod
    def _file_part(
        path: Path, filename: str | None = None, mime: str | None = None
    ) -> dict[str, str]:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"manus: file exceeds 20 MB limit: {path}")
        media_type = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "type": "file",
            "file_data": f"data:{media_type};base64,{encoded}",
            "filename": filename or path.name,
        }

    def _parts(self, prompt: str, files: list[Path]) -> list[dict[str, str]]:
        if len(prompt) <= 16_000:
            parts = [{"type": "text", "text": prompt}]
        else:
            encoded = base64.b64encode(prompt.encode()).decode("ascii")
            parts = [
                {
                    "type": "text",
                    "text": (
                        "Your full instructions are in the attached prompt.md. Follow them exactly."
                    ),
                },
                {
                    "type": "file",
                    "file_data": f"data:text/markdown;base64,{encoded}",
                    "filename": "prompt.md",
                },
            ]
        parts.extend(self._file_part(path) for path in files)
        return parts

    def _stop(self, task_id: str, key: str) -> None:
        # Stop remains retryable within a short fresh cleanup budget.
        deadline = getattr(self, "_deadline", None)
        self._deadline = time.monotonic() + 15
        try:
            self._request("POST", "/v2/task.stop", key, {"task_id": task_id})
        except (RuntimeError, OSError):
            pass
        finally:
            self._deadline = deadline

    def _poll(self, task_id: str, cwd: Path, key: str, profile: str, task_url: object):
        started = time.monotonic()
        minutes = float(agent_setting("manus", "max_wait_minutes", 30))
        self._deadline = started + minutes * 60
        path = manus_data_home() / f"{task_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        task_url = task_url or self._session_task_url(path)
        try:
            return self._poll_events(task_id, cwd, key, profile, task_url, started)
        except (RuntimeError, OSError) as exc:
            self._stop(task_id, key)
            output = str(exc).replace(key, "[redacted]")
            if isinstance(exc, TimeoutError):
                output = f"manus task was stopped after {minutes:g} minutes"
                if task_url:
                    output += f": {task_url}"
            return self._finish(task_id, cwd, path, started, output, 1, {}, task_url, profile, key)
        finally:
            self._deadline = None

    def _poll_events(
        self, task_id: str, cwd: Path, key: str, profile: str, task_url: object, started: float
    ) -> DispatchResult:
        poll_seconds = float(agent_setting("manus", "poll_seconds", 10))
        path = manus_data_home() / f"{task_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not task_url:
            task_url = self._session_task_url(path)
        seen = self._seen_ids(path)
        assistant: list[dict[str, Any]] = []
        latest_status: dict[str, Any] | None = None
        error_text = ""
        detail: dict[str, Any] = {}
        final_pass = False

        cursor: str | None = None
        last_id = None
        if path.exists():
            for raw in path.read_text().splitlines():
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("id"), str):
                    last_id = event["id"]
        while True:
            if time.monotonic() >= self._deadline:
                raise TimeoutError("manus polling deadline exceeded")
            while True:
                query: dict[str, object] = {"task_id": task_id, "order": "asc", "limit": 100}
                if cursor:
                    query["cursor"] = cursor
                elif last_id:
                    query["start_event_id"] = last_id
                page = self._request("GET", "/v2/task.listMessages", key, query=query)
                events = page.get("messages", [])
                if isinstance(events, list):
                    with path.open("a", encoding="utf-8") as stream:
                        for event in events:
                            if not isinstance(event, dict):
                                continue
                            event_id = self._event_key(event)
                            if isinstance(event.get("id"), str):
                                last_id = event["id"]
                            if event_id in seen:
                                continue
                            seen.add(event_id)
                            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                            if event.get("type") == "assistant_message" and isinstance(
                                event.get("assistant_message"), dict
                            ):
                                assistant.append(event["assistant_message"])
                            elif event.get("type") == "status_update" and isinstance(
                                event.get("status_update"), dict
                            ):
                                latest_status = event["status_update"]
                            elif event.get("type") == "error_message":
                                payload = event.get("error_message")
                                error_text = (
                                    payload.get("content", "")
                                    if isinstance(payload, dict)
                                    else str(payload or "")
                                )
                if isinstance(page.get("next_cursor"), str) and page["next_cursor"]:
                    cursor = page["next_cursor"]
                if not page.get("has_more"):
                    break
                next_cursor = page.get("next_cursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
                if time.monotonic() >= self._deadline:
                    raise TimeoutError("manus polling deadline exceeded")

            status = latest_status.get("agent_status") if latest_status else None
            if status == "waiting":
                detail_status = latest_status.get("status_detail")
                detail_status = detail_status if isinstance(detail_status, dict) else {}
                event_type = detail_status.get("waiting_for_event_type", "event")
                description = detail_status.get("waiting_description", "")
                output = f"[manus waiting: {event_type}] {description}".rstrip()
                last = self._last_assistant(assistant)
                if last:
                    output += "\n" + last
                return self._finish(
                    task_id, cwd, path, started, output, 0, detail, task_url, profile, key
                )
            if status == "error":
                return self._finish(
                    task_id,
                    cwd,
                    path,
                    started,
                    error_text or "Manus reported an error (no message)",
                    1,
                    detail,
                    task_url,
                    profile,
                    key,
                )
            if status == "stopped":
                response = self._request("GET", "/v2/task.detail", key, query={"task_id": task_id})
                raw_detail = response.get("task")
                detail = raw_detail if isinstance(raw_detail, dict) else {}
                task_url = detail.get("task_url", task_url)
                if detail.get("has_running_background_jobs") or detail.get("status") in {
                    "running",
                    "waiting",
                    "error",
                }:
                    final_pass = False
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("manus polling deadline exceeded")
                    time.sleep(min(poll_seconds, remaining))
                    continue
                if final_pass:
                    output = self._result_assistant(assistant)
                    output = self._download_attachments(
                        output, assistant, path.parent / task_id, key
                    )
                    return self._finish(
                        task_id, cwd, path, started, output, 0, detail, task_url, profile, key
                    )
                final_pass = True
                continue
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("manus polling deadline exceeded")
            time.sleep(min(poll_seconds, remaining))

    @staticmethod
    def _event_key(event: dict[str, Any]) -> str:
        event_id = event.get("id")
        if isinstance(event_id, str):
            return "id:" + event_id
        return "json:" + json.dumps(event, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _seen_ids(path: Path) -> set[str]:
        if not path.exists():
            return set()
        seen: set[str] = set()
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                seen.add(ManusHandler._event_key(event))
        return seen

    @staticmethod
    def _session_task_url(path: Path) -> object:
        if not path.exists():
            return None
        for raw in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "playmaker_meta":
                task_url = record.get("task_url")
                if task_url:
                    return task_url
        return None

    @staticmethod
    def _last_assistant(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            content = message.get("content")
            if isinstance(content, str):
                return content
        return ""

    @classmethod
    def _result_assistant(cls, messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("delivery_kind") == "result" and isinstance(message.get("content"), str):
                return message["content"]
        return cls._last_assistant(messages)

    def _download_attachments(
        self, output: str, messages: list[dict[str, Any]], session_dir: Path, key: str
    ) -> str:
        attachments: list[dict[str, Any]] = []
        for message in messages:
            raw = message.get("attachments")
            if isinstance(raw, list):
                attachments.extend(item for item in raw if isinstance(item, dict))
        paths: list[str] = []
        for index, attachment in enumerate(attachments, 1):
            url, filename = attachment.get("url"), attachment.get("filename")
            safe_name = (
                Path(filename).name
                if isinstance(filename, str) and filename
                else f"attachment-{index}"
            )
            try:
                if not isinstance(url, str) or not url:
                    raise ValueError("missing attachment URL")
                files_dir = session_dir / "files"
                files_dir.mkdir(parents=True, exist_ok=True)
                if safe_name in {"", ".", ".."}:
                    safe_name = f"attachment-{index}"
                target = files_dir / safe_name
                suffix = 2
                while target.exists():
                    name = Path(safe_name)
                    target = files_dir / f"{name.stem}-{suffix}{name.suffix}"
                    suffix += 1
                request = urllib.request.Request(url)
                with urllib.request.urlopen(request, timeout=60) as response:
                    target.write_bytes(response.read())
            except Exception:
                # Downloads are optional; one bad URL/read/write must not lose the result.
                paths.append(f"failed: {safe_name}")
                continue
            paths.append(str(target))
        if paths:
            output += ("\n" if output else "") + "Attachments: " + ", ".join(paths)
        return output

    def _finish(
        self,
        task_id: str,
        cwd: Path,
        path: Path,
        started: float,
        output: str,
        exit_code: int,
        detail: dict[str, Any],
        task_url: object,
        profile: str,
        key: str,
    ) -> DispatchResult:
        if not detail:
            deadline = getattr(self, "_deadline", None)
            self._deadline = time.monotonic() + 15
            try:
                response = self._request("GET", "/v2/task.detail", key, query={"task_id": task_id})
                raw_detail = response.get("task")
                detail = raw_detail if isinstance(raw_detail, dict) else {}
            except (RuntimeError, OSError):
                detail = {}
            finally:
                self._deadline = deadline
        credit = detail.get("credit_usage")
        try:
            credit = int(str(credit)) if credit is not None else None
        except (TypeError, ValueError):
            credit = None
        meta = {
            "type": "playmaker_meta",
            "credit_usage": credit,
            "task_url": detail.get("task_url", task_url),
            "agent_profile": detail.get("agent_profile", profile),
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(meta, ensure_ascii=False) + "\n")
        return DispatchResult(
            agent_session_id=task_id,
            cwd=str(cwd),
            session_file=path,
            initial_output=output,
            cost_usd=None,
            duration_seconds=time.monotonic() - started,
            exit_code=exit_code,
        )

    def _request(
        self,
        method: str,
        path: str,
        key: str,
        body: dict[str, Any] | None = None,
        *,
        query: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        url = BASE_URL + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None and method != "GET" else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"x-manus-api-key": key, "Content-Type": "application/json"},
        )
        delay = 1.0
        deadline = getattr(self, "_deadline", None)
        if deadline is None:
            deadline = time.monotonic() + float(agent_setting("manus", "max_wait_minutes", 30)) * 60
        idempotent = method == "GET" or path == "/v2/task.stop"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("manus request deadline exceeded")
            retry = False
            try:
                with urllib.request.urlopen(request, timeout=min(60, remaining)) as response:
                    payload = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                try:
                    payload = json.loads(exc.read())
                except (ValueError, OSError, http.client.HTTPException):
                    payload = {}
                error = payload.get("error") if isinstance(payload, dict) else None
                code = error.get("code", exc.code) if isinstance(error, dict) else exc.code
                message = (
                    error.get("message", "request failed")
                    if isinstance(error, dict)
                    else "request failed"
                )
                retry = exc.code == 429 or code == "rate_limited"
                retry = retry or (idempotent and 500 <= exc.code < 600)
                if not retry:
                    raise RuntimeError(
                        f"manus API error ({code}): {message}".replace(key, "[redacted]")
                    ) from None
            except http.client.HTTPException:
                if method != "GET":
                    raise RuntimeError("manus API request failed; acceptance unknown") from None
                retry = True
            except (OSError, urllib.error.URLError):
                if not idempotent:
                    raise RuntimeError("manus API request failed; acceptance unknown") from None
                retry = True
            except (ValueError, UnicodeDecodeError):
                if method != "GET":
                    raise RuntimeError("manus API returned invalid JSON") from None
                retry = True
            if not retry:
                if not isinstance(payload, dict):
                    raise RuntimeError("manus API returned a non-object response")
                if payload.get("ok") is not False:
                    return payload
                error = payload.get("error")
                code = error.get("code") if isinstance(error, dict) else "unknown"
                if code != "rate_limited":
                    message = error.get("message") if isinstance(error, dict) else "request failed"
                    raise RuntimeError(
                        f"manus API error ({code}): {message}".replace(key, "[redacted]")
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("manus request deadline exceeded")
            time.sleep(min(delay + random.uniform(0, delay / 4), 30, remaining))
            delay = min(delay * 2, 30)

    def find_session_file(self, agent_session_id: str, cwd: Path) -> Path | None:
        path = manus_data_home() / f"{agent_session_id}.jsonl"
        return path if path.is_file() else None

    def parse_session_file(self, path: Path) -> list[Turn]:
        return parse_session_file(path)

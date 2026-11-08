"""Project cost reporting and quota snapshots.

The reporting helpers deliberately have no provider calls.  The only command
which may refresh quota data is ``snapshot`` and it follows the same freshness
rule as ``playmaker quotas``.
"""

from __future__ import annotations

import json
import math
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from playmaker import config, state

CLAUDE_PROJECTS_PATH = Path("~/.claude/projects").expanduser()
LAUNCH_AGENT_PATH = Path("~/Library/LaunchAgents/dev.playmaker.costs-snapshot.plist").expanduser()
USAGE_CACHE_VERSION = 1
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)


def costs_dir() -> Path:
    return state.PLAYMAKER_HOME / "costs"


PRICE_TABLE: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10, 50, 0.25),
    "claude-fable-5": (10, 50, 1),
    "claude-mythos-5-1": (10, 50, 0.25),
    "claude-opus-5-5": (4, 20, 0.2),
    "claude-opus-5": (5, 25, 0.5),
    "claude-opus-4-8": (5, 25, 0.5),
    "claude-opus-4-7": (5, 25, 0.5),
    "claude-opus-4-6": (5, 25, 0.5),
    "claude-sonnet-5-5": (2, 10, 0.2),
    "claude-sonnet-5": (2, 10, 0.2),
    "claude-sonnet-4-6": (3, 15, 0.3),
    "claude-haiku-5-5": (0.1, 0.5, 0.01),
    "claude-haiku-4-5": (1, 5, 0.1),
}


class CostsError(ValueError):
    """A user-facing costs configuration or selection error."""


def _parse_time(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _parse_day(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise CostsError(f"invalid {field} date: {value!r}") from exc


def _path_under(value: object, root: Path) -> bool:
    if not value:
        return False
    try:
        candidate = Path(str(value)).expanduser().resolve()
        root = root.expanduser().resolve()
        return candidate == root or root in candidate.parents
    except OSError:
        return False


def session_matches_project(row: dict[str, Any], root: Path) -> bool:
    """Whether a database row belongs to a repository, including its worktrees."""
    root = root.expanduser().resolve()
    if str(row.get("repo_root") or "") == str(root):
        return True
    special = (Path(f"{root}-wt"), root / ".claude" / "worktrees")
    for field in ("cwd", "worktree_path"):
        value = row.get(field)
        if _path_under(value, root) or any(_path_under(value, item) for item in special):
            return True
    return False


def load_sessions(db_path: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Read sessions without initializing or modifying the database."""
    warnings: list[str] = []
    db_path = db_path or state.DB_PATH
    if not db_path.exists():
        return [], warnings
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM sessions")]
        conn.close()
        return rows, warnings
    except sqlite3.Error as exc:
        warnings.append(f"could not read sessions database: {exc}")
        return [], warnings


def resolve_project(value: str, rows: list[dict[str, Any]]) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or "/" in value or value in {".", ".."}:
        return candidate.resolve()
    roots: set[Path] = set()
    for key in ("repo_root", "cwd"):
        for row in rows:
            raw = row.get(key)
            if raw:
                path = Path(str(raw)).expanduser().resolve()
                in_worktree = any(parent.name.endswith("-wt") for parent in path.parents) or any(
                    parent.name == "worktrees" and parent.parent.name == ".claude"
                    for parent in path.parents
                )
                if path.name == value and not in_worktree:
                    roots.add(path)
        if roots:
            break
    if len(roots) == 1:
        return next(iter(roots))
    choices = ", ".join(str(item) for item in sorted(roots)) or "none"
    if len(roots) > 1:
        raise CostsError(f"project name {value!r} is ambiguous: {choices}")
    raise CostsError(f"project {value!r} was not found (candidates: {choices})")


def _read_json_lines(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    except (OSError, UnicodeError) as exc:
        warnings.append(f"could not read {path}: {exc}")
        return result
    bad = 0
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(item, dict):
            result.append(item)
        else:
            bad += 1
    if bad:
        warnings.append(f"skipped {bad} malformed line(s) in {path}")
    return result


def _number(value: object) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )
    except OverflowError:
        return False


def load_subscriptions(
    path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, tuple[float, float, float]]]:
    path = path if path is not None else costs_dir() / "subscriptions.toml"
    if not path.exists():
        return [], {}
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CostsError(f"invalid subscriptions file: {exc}") from exc
    if set(raw) - {"plan", "api_prices"}:
        raise CostsError("unknown top-level key in subscriptions file")
    prices = raw.get("api_prices", {})
    if not isinstance(prices, dict):
        raise CostsError("api_prices must be a table")
    overrides = {}
    for model, price in prices.items():
        if (
            not isinstance(price, dict)
            or set(price) != {"input", "output", "cache_read"}
            or not all(_number(value) for value in price.values())
        ):
            raise CostsError(
                f"invalid api price for {model}: expected input/output/cache_read numbers"
            )
        overrides[model] = (
            float(price["input"]),
            float(price["output"]),
            float(price["cache_read"]),
        )
    plans = raw.get("plan", [])
    if not isinstance(plans, list):
        raise CostsError("plan must be an array of tables")
    allowed = {
        "name",
        "group",
        "lanes",
        "model_prefixes",
        "monthly_usd",
        "start",
        "end",
        "status",
        "note",
        "usd_per_credit",
    }
    output: list[dict[str, Any]] = []
    for index, plan in enumerate(plans, 1):
        if not isinstance(plan, dict) or set(plan) - allowed:
            unknown = sorted(set(plan) - allowed) if isinstance(plan, dict) else []
            raise CostsError(
                f"plan {index}: unknown or invalid keys: {', '.join(unknown) or 'entry'}"
            )
        required = {"name", "group", "lanes", "start"}
        if missing := required - set(plan):
            raise CostsError(f"plan {index}: missing {', '.join(sorted(missing))}")
        item = dict(plan)
        item["start"] = _parse_day(item["start"], "start")
        item["end"] = _parse_day(item["end"], "end") if item.get("end") else None
        if item["end"] and item["end"] <= item["start"]:
            raise CostsError(f"plan {index}: end must be after start")
        if item.get("status", "active") not in {"active", "pending"}:
            raise CostsError(f"plan {index}: status must be active or pending")
        item.setdefault("status", "active")
        item.setdefault("model_prefixes", [])
        item.setdefault("note", "")
        if (
            not isinstance(item["lanes"], list)
            or not isinstance(item["model_prefixes"], list)
            or not item["lanes"]
            or not all(isinstance(value, str) for value in item["lanes"] + item["model_prefixes"])
            or len(set(item["lanes"])) != len(item["lanes"])
        ):
            raise CostsError(f"plan {index}: lanes and model_prefixes must be lists")
        for field in ("monthly_usd", "usd_per_credit"):
            if item.get(field) is not None:
                if not _number(item[field]):
                    raise CostsError(f"plan {index}: {field} must be a finite nonnegative number")
                item[field] = float(item[field])
        output.append(item)
    for group in {str(item["group"]) for item in output}:
        grouped = sorted(
            (item for item in output if str(item["group"]) == group and item["status"] == "active"),
            key=lambda x: x["start"],
        )
        for previous, current in zip(grouped, grouped[1:], strict=False):
            if previous["end"] is None or current["start"] < previous["end"]:
                raise CostsError(f"overlapping dates in plan group {group!r}")
    return output, overrides


def load_plans(path: Path | None = None) -> list[dict[str, Any]]:
    return load_subscriptions(path)[0]


def _in_period(timestamp: object, since: date, until: date) -> bool:
    parsed = _parse_time(timestamp)
    return bool(parsed and since <= parsed.date() < until)


def _session_weight(row: dict[str, Any]) -> float | None:
    value = row.get("duration_seconds")
    try:
        if value is not None and math.isfinite(float(value)) and float(value) > 0:
            return float(value)
    except (TypeError, ValueError):
        pass
    start, finish = _parse_time(row.get("started_at")), _parse_time(row.get("finished_at"))
    if start and finish and finish > start:
        return (finish - start).total_seconds()
    return None


def _month_slices(since: date, until: date) -> list[tuple[date, date, int]]:
    current = since.replace(day=1)
    result = []
    while current < until:
        following = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        result.append((max(since, current), min(until, following), (following - current).days))
        current = following
    return result


def _model_name(model: object) -> str:
    return str(model or "unknown").split("[")[0].strip()


def _price_for(
    model: str, overrides: dict[str, tuple[float, float, float]]
) -> tuple[float, float, float] | None:
    choices = {**PRICE_TABLE, **overrides}
    matches = [key for key in choices if model.startswith(key)]
    return choices[max(matches, key=len)] if matches else None


# Validate skipped lines without constructing their large message/tool payloads.
# Only usage-bearing lines go through json.loads; malformed non-usage lines still
# count toward the per-file warning. The regex scans string contents in C.
_JSON_TOKEN = re.compile(
    r'[ \t\r\n]*("(?:[^"\\\x00-\x1f]++|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*+"'
    r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    r"|true|false|null|[{}\[\],:])"
)


def _valid_json_object(line: str) -> bool:
    first = _JSON_TOKEN.match(line)
    if first is None or first[1] != "{":
        return False
    stack = [("}", "key_or_end")]
    offset = first.end()
    while stack:
        token = _JSON_TOKEN.match(line, offset)
        if token is None:
            return False
        value, offset = token[1], token.end()
        closing, expecting = stack[-1]
        if expecting in {"key", "key_or_end"}:
            if expecting == "key_or_end" and value == closing:
                stack.pop()
            elif value.startswith('"'):
                stack[-1] = (closing, "colon")
            else:
                return False
        elif expecting == "colon":
            if value != ":":
                return False
            stack[-1] = (closing, "value")
        elif expecting == "after":
            if value == closing:
                stack.pop()
            elif value == ",":
                stack[-1] = (closing, "key" if closing == "}" else "value")
            else:
                return False
        elif expecting == "value_or_end" and value == closing:
            stack.pop()
        else:
            stack[-1] = (closing, "after")
            if value == "{":
                stack.append(("}", "key_or_end"))
            elif value == "[":
                stack.append(("]", "value_or_end"))
            elif value in {"}", "]", ",", ":"}:
                return False
    return not line[offset:].strip(" \t\r\n")


def _compact_usage(record: dict[str, Any]) -> dict[str, Any] | None:
    message = record.get("message")
    if not isinstance(message, dict) or "usage" not in message:
        return None
    model = _model_name(message.get("model"))
    if model == "<synthetic>":
        return None
    usage = message["usage"]
    stamp = _parse_time(record.get("timestamp"))
    if (
        not isinstance(usage, dict)
        or not stamp
        or not isinstance(message.get("id"), str)
        or not message["id"]
        or not isinstance(record.get("cwd"), str)
    ):
        raise ValueError("invalid usage record")
    creation = usage.get("cache_creation")
    if creation is None:
        creation = {}
    if not isinstance(creation, dict):
        raise ValueError("invalid cache creation")
    tokens = [
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        usage.get("cache_read_input_tokens", 0),
        usage.get("cache_creation_input_tokens", 0)
        if not creation
        else creation.get("ephemeral_5m_input_tokens", 0),
        creation.get("ephemeral_1h_input_tokens", 0),
    ]
    tokens = [0 if value is None else value for value in tokens]
    if not all(_number(value) for value in tokens):
        raise ValueError("invalid usage tokens")
    return {
        "id": message["id"],
        "session": str(record.get("sessionId") or ""),
        "cwd": record["cwd"],
        "day": stamp.date().isoformat(),
        "model": model,
        "tokens": tokens,
    }


def _read_claude_file(path: Path) -> dict[str, Any]:
    """Injectable reader; cache only compact usage and the malformed-line count."""
    records = []
    bad = 0
    with path.open("rb") as stream:
        for raw_line in stream:
            try:
                line = raw_line.decode("utf-8")
                if '"usage"' not in line:
                    bad += not _valid_json_object(line)
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("not an object")
                compact = _compact_usage(record)
                if compact is not None:
                    records.append(compact)
            except (ValueError, TypeError, RecursionError):
                bad += 1
    return {"records": records, "malformed": bad}


def _valid_cached_file(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    if not all(type(entry.get(key)) is int for key in ("size", "mtime_ns", "malformed")):
        return False
    if entry["size"] < 0 or entry["malformed"] < 0 or not isinstance(entry.get("records"), list):
        return False
    for record in entry["records"]:
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) for key in ("id", "session", "cwd", "day", "model")
        ):
            return False
        try:
            if date.fromisoformat(record["day"]).isoformat() != record["day"]:
                return False
        except ValueError:
            return False
        tokens = record.get("tokens")
        if (
            not record["id"]
            or not isinstance(tokens, list)
            or len(tokens) != len(TOKEN_FIELDS)
            or not all(_number(value) for value in tokens)
        ):
            return False
    return True


def _claude_records(since: date | None, warnings: list[str]) -> list[dict[str, Any]]:
    if not CLAUDE_PROJECTS_PATH.exists():
        return []
    cache_path = costs_dir() / "claude-usage-cache.json"
    cached = {}
    changed = False
    try:
        document = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("version") != USAGE_CACHE_VERSION
            or not isinstance(document.get("files"), dict)
        ):
            raise ValueError("invalid cache or version")
        cached = document["files"]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError) as exc:
        warnings.append(f"rebuilding Claude usage cache {cache_path}: {exc}")
        changed = True

    cutoff = datetime.combine(since, datetime.min.time(), UTC).timestamp() if since else None
    files = {}
    records = []
    for path in sorted(CLAUDE_PROJECTS_PATH.rglob("*.jsonl")):
        key = str(path.resolve())
        entry = cached.get(key)
        try:
            stat = path.stat()
            if cutoff is not None and stat.st_mtime < cutoff:
                # Preserve earlier cached usage for later reports with a wider period.
                if entry is not None:
                    files[key] = entry
                continue
            if (
                not isinstance(entry, dict)
                or entry.get("size") != stat.st_size
                or entry.get("mtime_ns") != stat.st_mtime_ns
                or not _valid_cached_file(entry)
            ):
                if entry is not None and not _valid_cached_file(entry):
                    warnings.append(f"rebuilding malformed Claude usage cache entry for {path}")
                entry = {
                    **_read_claude_file(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                changed = True
            files[key] = entry
            records.extend(entry["records"])
            if entry["malformed"]:
                warnings.append(f"skipped {entry['malformed']} malformed line(s) in {path}")
        except OSError as exc:
            warnings.append(f"could not read {path}: {exc}")

    if changed or files.keys() != cached.keys():
        temporary = None
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cache_path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    {"version": USAGE_CACHE_VERSION, "files": files},
                    stream,
                    separators=(",", ":"),
                )
            temporary.replace(cache_path)
        except OSError as exc:
            warnings.append(f"could not write Claude usage cache {cache_path}: {exc}")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return records


def _excluded_claude_sessions(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(row["agent_session_id"])
        for row in rows
        if row.get("agent") == "claude-api" and row.get("agent_session_id")
    }


def claude_usage(
    project_root: Path,
    rows: list[dict[str, Any]],
    since: date,
    until: date,
    warnings: list[str],
    overrides: dict[str, tuple[float, float, float]] | None = None,
    *,
    records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggregate cached usage, retaining global response-ID deduplication."""
    if records is None:
        records = _claude_records(since, warnings)
    excluded = _excluded_claude_sessions(rows)
    best_by_id: dict[str, dict[str, Any]] = {}
    left, right = since.isoformat(), until.isoformat()
    for record in records:
        if (
            record["session"] in excluded
            or not left <= record["day"] < right
            or record["model"] == "<synthetic>"
        ):
            continue
        identifier = record["id"]
        output_tokens = record["tokens"][1]
        if identifier not in best_by_id or output_tokens > best_by_id[identifier]["tokens"][1]:
            best_by_id[identifier] = record
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    all_by_day: dict[str, float] = defaultdict(float)
    project_by_day: dict[str, float] = defaultdict(float)
    project_counts: dict[str, int] = defaultdict(int)
    belongs_by_cwd = {}
    prices = {}
    for record in best_by_id.values():
        identifier, day, model = record["id"], record["day"], record["model"]
        if model not in prices:
            prices[model] = _price_for(model, overrides or {})
        price = prices[model]
        tokens = record["tokens"]
        cost = (
            0.0
            if price is None
            else (
                tokens[0] * price[0]
                + tokens[1] * price[1]
                + tokens[2] * price[2]
                + tokens[3] * price[0] * 1.25
                + tokens[4] * price[0] * 2
            )
            / 1_000_000
        )
        all_by_day[day] += cost
        cwd = record["cwd"]
        if cwd not in belongs_by_cwd:
            belongs_by_cwd[cwd] = session_matches_project({"cwd": cwd}, project_root)
        if belongs_by_cwd[cwd]:
            for key, value in zip(TOKEN_FIELDS, tokens, strict=True):
                by_model[model][key] += value
            by_model[model]["api_equivalent_usd"] += cost
            project_by_day[day] += cost
            project_counts[day] += 1
    all_cost, project_cost = sum(all_by_day.values()), sum(project_by_day.values())
    return {
        "models": {model: {**values, "price": prices[model]} for model, values in by_model.items()},
        "api_equivalent_usd": project_cost,
        "share": project_cost / all_cost if all_cost else 0.0,
        "by_day": dict(project_by_day),
        "counts_by_day": dict(project_counts),
    }, {"all": all_cost, "by_day": dict(all_by_day)}


def _plan_eligible(row: dict[str, Any], plan: dict[str, Any]) -> bool:
    return row.get("agent") in plan["lanes"] and (
        not plan["model_prefixes"]
        or any(str(row.get("model") or "").startswith(prefix) for prefix in plan["model_prefixes"])
    )


def _split_lane_charge(
    amount: float, lanes: list[str], project_rows: list[dict[str, Any]], claude_count: int
) -> dict[str, float]:
    if "claude-code" in lanes:
        return {lane: (amount if lane == "claude-code" else 0.0) for lane in lanes}
    weights = {
        lane: sum(_session_weight(row) or 0 for row in project_rows if row.get("agent") == lane)
        for lane in lanes
    }
    if not sum(weights.values()):
        weights = {
            lane: (
                claude_count
                if lane == "claude-code"
                else sum(row.get("agent") == lane for row in project_rows)
            )
            for lane in lanes
        }
    if not sum(weights.values()):
        weights = dict.fromkeys(lanes, 1)
    total = sum(weights.values())
    return {lane: amount * weight / total for lane, weight in weights.items()}


def _manus_usage(
    rows: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    since: date,
    until: date,
    warnings: list[str],
) -> tuple[float, dict[str, float]]:
    credits_total = 0.0
    by_day: dict[str, float] = defaultdict(float)
    seen = set()
    # Resumes share a task ID. Its first session start is the task's billing date.
    tasks = sorted(
        (row for row in rows if row.get("agent") == "manus" and _parse_time(row.get("started_at"))),
        key=lambda row: _parse_time(row["started_at"]),
    )
    for row in tasks:
        choices = []
        if row.get("agent_session_id"):
            choices.append(state.PLAYMAKER_HOME / "manus" / f"{row['agent_session_id']}.jsonl")
        if row.get("session_file_path"):
            choices.append(Path(row["session_file_path"]))
        if not choices:
            continue
        task_keys = {str(path.resolve()) for path in choices}
        if row.get("agent_session_id"):
            task_keys.add(str(row["agent_session_id"]))
        if seen & task_keys:
            continue
        seen.update(task_keys)
        day = _parse_time(row["started_at"]).date()
        if not since <= day < until:
            continue
        credits = None
        for task in dict.fromkeys(choices):
            for record in _read_json_lines(task, warnings):
                if record.get("type") != "playmaker_meta":
                    continue
                value = record.get("credit_usage")
                if _number(value):
                    credits = float(value)
                elif value is not None:
                    warnings.append(f"invalid Manus credit usage in {task}")
            if credits is not None:
                break
        if credits is None:
            continue
        credits_total += credits
        active = [
            plan
            for plan in plans
            if plan["status"] == "active"
            and plan["start"] <= day
            and (plan["end"] is None or day < plan["end"])
            and _plan_eligible(row, plan)
            and plan.get("usd_per_credit") is not None
        ]
        if len(active) > 1:
            names = ", ".join(str(plan["name"]) for plan in active)
            raise CostsError(f"ambiguous Manus credit plans on {day}: {names}")
        if active:
            by_day[day.isoformat()] += credits * active[0]["usd_per_credit"]
    return credits_total, dict(by_day)


def build_report(
    project: str,
    since: date | None = None,
    until: date | None = None,
    *,
    db_path: Path | None = None,
    subscriptions_path: Path | None = None,
) -> dict[str, Any]:
    rows, warnings = load_sessions(db_path)
    root = resolve_project(project, rows)
    matched = [row for row in rows if session_matches_project(row, root)]
    until = until or date.today() + timedelta(days=1)
    if since is not None and until <= since:
        raise CostsError("until must be after since")
    plans, overrides = load_subscriptions(subscriptions_path)
    records = _claude_records(since, warnings)
    if since is None:
        excluded = _excluded_claude_sessions(rows)
        dated = [
            stamp.date()
            for row in matched
            if (stamp := _parse_time(row.get("started_at"))) and stamp.date() < until
        ]
        belongs = {}
        for record in records:
            cwd = record["cwd"]
            if cwd not in belongs:
                belongs[cwd] = session_matches_project({"cwd": cwd}, root)
            if (
                record["session"] not in excluded
                and belongs[cwd]
                and record["day"] < until.isoformat()
            ):
                dated.append(date.fromisoformat(record["day"]))
        since = min(dated, default=date.today())
    if until <= since:
        raise CostsError("until must be after since")
    period_rows = [row for row in rows if _in_period(row.get("started_at"), since, until)]
    project_rows = [row for row in matched if _in_period(row.get("started_at"), since, until)]
    claude, all_claude = claude_usage(
        root, rows, since, until, warnings, overrides, records=records
    )
    metered: dict[str, float] = defaultdict(float)
    days: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    skipped_lanes: dict[str, float] = defaultdict(float)
    for row in project_rows:
        try:
            amount = float(row.get("cost_usd") or 0)
        except (TypeError, ValueError):
            amount = 0
        if not math.isfinite(amount) or amount <= 0:
            continue
        lane = str(row.get("agent") or "")
        if lane in {"claude-api", "perplexity"}:
            day = _parse_time(row["started_at"]).date().isoformat()
            metered[lane] += amount
            days[day][lane] += amount
        elif lane != "claude":
            skipped_lanes[lane] += amount
    for lane, amount in sorted(skipped_lanes.items()):
        warnings.append(f"skipped ${amount:.2f} recorded cost_usd on lane {lane}")
    credits, manus_days = _manus_usage(matched, plans, since, until, warnings)
    if credits or manus_days:
        metered["manus_credits"] = credits
    if manus_days:
        metered["manus"] = sum(manus_days.values())
        for day, amount in manus_days.items():
            days[day]["manus"] += amount

    subscriptions: list[dict[str, Any]] = []
    unattributed: dict[str, float] = defaultdict(float)
    subscription_lanes: dict[str, float] = defaultdict(float)
    for plan in plans:
        state_label = (
            "pending"
            if plan["status"] == "pending"
            else (
                "price missing"
                if plan.get("monthly_usd") is None
                else ("ended" if plan.get("end") and plan["end"] <= date.today() else "active")
            )
        )
        charged = 0.0
        if plan["status"] == "active" and plan.get("monthly_usd") is not None:
            for left, right, month_days in _month_slices(since, until):
                start, end = max(left, plan["start"]), min(right, plan["end"] or right)
                if start >= end:
                    continue
                overlap_days = (end - start).days
                cost = plan["monthly_usd"] * overlap_days / month_days
                all_items = [
                    row
                    for row in period_rows
                    if _plan_eligible(row, plan) and _in_period(row.get("started_at"), start, end)
                ]
                mine_items = [
                    row
                    for row in project_rows
                    if _plan_eligible(row, plan) and _in_period(row.get("started_at"), start, end)
                ]
                if "claude-code" in plan["lanes"]:
                    slice_days = [
                        day
                        for day in all_claude["by_day"]
                        if start.isoformat() <= day < end.isoformat()
                    ]
                    total = sum(all_claude["by_day"][day] for day in slice_days)
                    mine = sum(claude["by_day"].get(day, 0.0) for day in slice_days)
                else:
                    all_weights = [_session_weight(row) for row in all_items]
                    if any(value is not None for value in all_weights):
                        total = sum(value or 0 for value in all_weights)
                        mine = sum(_session_weight(row) or 0 for row in mine_items)
                    else:
                        total, mine = len(all_items), len(mine_items)
                share = mine / total if total else 0.0
                part = cost * share
                charged += part
                if not total and cost:
                    unattributed[str(plan["name"])] += cost
                claude_count = sum(
                    count
                    for day, count in claude["counts_by_day"].items()
                    if start.isoformat() <= day < end.isoformat()
                )
                for lane, amount in _split_lane_charge(
                    part, plan["lanes"], mine_items, claude_count
                ).items():
                    if amount:
                        subscription_lanes[lane] += amount
                if part:
                    for offset in range(overlap_days):
                        day = (start + timedelta(days=offset)).isoformat()
                        days[day][f"subscription:{plan['name']}"] += part / overlap_days
        subscriptions.append(
            {
                "name": plan["name"],
                "group": plan["group"],
                "status": plan["status"],
                "state": state_label,
                "charged_usd": charged,
                "monthly_usd": plan.get("monthly_usd"),
            }
        )
    metered_total = sum(amount for lane, amount in metered.items() if lane != "manus_credits")
    subscription_total = sum(item["charged_usd"] for item in subscriptions)
    total_lanes = {
        lane: amount
        for lane, amount in metered.items()
        if lane != "manus_credits" and amount
    }
    for lane, amount in subscription_lanes.items():
        if amount:
            total_lanes[lane] = total_lanes.get(lane, 0.0) + amount
    total_lanes = {lane: amount for lane, amount in total_lanes.items() if amount}
    return {
        "project": str(root),
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "metered": {"lanes": dict(metered)},
        "subscriptions": {
            "plans": subscriptions,
            "unattributed_usd": {name: amount for name, amount in unattributed.items() if amount},
        },
        "claude_code": claude,
        "totals": {
            "lanes": total_lanes,
            "by_day": {key: dict(value) for key, value in days.items()},
            "metered_usd": metered_total,
            "subscriptions_usd": subscription_total,
            "spent_usd": metered_total + subscription_total,
            "claude_code_api_equivalent_usd": claude["api_equivalent_usd"],
        },
        "inputs": {
            "sessions": len(project_rows),
            "all_claude_code_api_equivalent_usd": all_claude["all"],
        },
        "warnings": warnings,
    }


def _quota_payload(warnings: list[str]) -> dict[str, Any] | None:
    """Use the normal quotas cache freshness policy, without forcing a probe."""
    from playmaker import quotas

    path = state.QUOTAS_PATH
    age = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            warnings.append(f"skipped malformed quota cache {path}: expected an object")
            return None
        fetched = _parse_time(payload.get("fetched_at"))
        age = (datetime.now(UTC) - fetched).total_seconds() if fetched else None
    except FileNotFoundError:
        payload = None
    except (OSError, ValueError) as exc:
        warnings.append(f"skipped malformed quota cache {path}: {exc}")
        return None
    max_age = state.parse_duration(config.setting("quotas", "max_age", "5m"), 300)
    if payload is None or age is None or age > max_age:
        try:
            quotas.refresh_all(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"could not refresh quota cache {path}: {exc}")
            return None
    if not isinstance(payload, dict) or not _parse_time(payload.get("fetched_at")):
        warnings.append(f"skipped malformed quota cache {path}: expected an object with fetched_at")
        return None
    return payload


def snapshot(warnings: list[str] | None = None) -> bool:
    warnings = warnings if warnings is not None else []
    payload = _quota_payload(warnings)
    if payload is None:
        return False
    path = costs_dir() / "quota-snapshots.jsonl"
    last = None
    bad = 0
    for record in _read_json_lines(path, warnings):
        quota = record.get("quotas")
        if not isinstance(quota, dict) or not _parse_time(quota.get("fetched_at")):
            bad += 1
        else:
            last = quota
    if bad:
        warnings.append(f"skipped {bad} malformed snapshot line(s) in {path}")
    if last is not None and last["fetched_at"] == payload["fetched_at"]:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"taken_at": datetime.now(UTC).isoformat(), "quotas": payload}) + "\n"
        )
    return True


def run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], check=True, text=True, capture_output=True)


def agent_plist(interval: int) -> bytes:
    executable = shutil.which("playmaker") or os.path.abspath(sys.argv[0])
    content = {
        "Label": "dev.playmaker.costs-snapshot",
        "ProgramArguments": [str(Path(executable).resolve()), "costs", "snapshot"],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(state.LOGS_DIR / "costs-snapshot.log"),
        "StandardErrorPath": str(state.LOGS_DIR / "costs-snapshot.log"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
    }
    return plistlib.dumps(content)


costs_app = typer.Typer(
    help="Report per-project development costs.", invoke_without_command=True, no_args_is_help=False
)


@costs_app.callback()
def costs_command(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project"),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if not project:
        raise typer.BadParameter("--project is required")
    try:
        report = build_report(
            project,
            _parse_day(since, "since") if since else None,
            _parse_day(until, "until") if until else None,
        )
    except CostsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if json_out:
        typer.echo(json.dumps(report, indent=2, default=str))
        return
    total = report["totals"]
    typer.echo("Metered")
    for lane, amount in sorted(report["metered"]["lanes"].items()):
        suffix = " credits" if lane == "manus_credits" else ""
        typer.echo(f"  {lane}: {amount:.2f}{suffix}" if suffix else f"  {lane}: ${amount:.2f}")
    typer.echo("Subscriptions")
    for plan in report["subscriptions"]["plans"]:
        price = plan["monthly_usd"]
        price_label = "price missing" if price is None else f"${price:.2f}/month"
        typer.echo(f"  {plan['name']}: ${plan['charged_usd']:.2f} ({plan['state']}; {price_label})")
    for name, amount in sorted(report["subscriptions"]["unattributed_usd"].items()):
        typer.echo(f"  Unattributed — {name}: ${amount:.2f}")
    typer.echo("Claude Code")
    typer.echo(f"  Project share: {report['claude_code']['share']:.2%}")
    for model, usage in sorted(report["claude_code"]["models"].items()):
        price = usage["price"]
        price_label = "price unknown" if price is None else "price known"
        typer.echo(
            f"  {model}: input {usage['input_tokens']:.0f}, output {usage['output_tokens']:.0f}, "
            f"cache read {usage['cache_read_input_tokens']:.0f}, "
            f"cache write 5m {usage['cache_write_5m_tokens']:.0f}, "
            f"cache write 1h {usage['cache_write_1h_tokens']:.0f}; {price_label}"
        )
    typer.echo("Totals")
    for lane, amount in sorted(total["lanes"].items()):
        typer.echo(f"  {lane}: ${amount:.2f}")
    for day, amounts in sorted(total["by_day"].items()):
        typer.echo(f"  {day}: ${sum(amounts.values()):.2f}")
    missing_prices = [
        plan["name"]
        for plan in report["subscriptions"]["plans"]
        if plan.get("status") == "active" and plan["monthly_usd"] is None
    ]
    if missing_prices:
        typer.echo(f"Plans with price not set: {', '.join(missing_prices)}")
    claude_cost = total["claude_code_api_equivalent_usd"]
    typer.echo(f"Claude Code API-equivalent: ${claude_cost:.2f} (information only)")
    summary = (
        f"spent on {report['project']} so far: ${total['spent_usd']:.2f} "
        f"(metered ${total['metered_usd']:.2f} + subscriptions "
        f"${total['subscriptions_usd']:.2f}; Claude Code API-equivalent "
        f"${claude_cost:.2f}, information only)"
    )
    typer.echo(summary)
    for warning in report["warnings"]:
        typer.echo(f"Warning: {warning}", err=True)


@costs_app.command("plans")
def plans_command() -> None:
    try:
        plans = load_plans()
    except CostsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    for plan in plans:
        label = (
            "pending"
            if plan["status"] == "pending"
            else (
                "price missing"
                if plan.get("monthly_usd") is None
                else ("ended" if plan.get("end") and plan["end"] <= date.today() else "active")
            )
        )
        typer.echo(f"{plan['name']}: {label}")


@costs_app.command("snapshot")
def snapshot_command() -> None:
    warnings: list[str] = []
    appended = snapshot(warnings)
    typer.echo("snapshot appended" if appended else "snapshot unchanged")
    for warning in warnings:
        typer.echo(f"Warning: {warning}", err=True)


@costs_app.command("install-agent")
def install_agent(
    interval: int = typer.Option(3600, "--interval"),
    print_only: bool = typer.Option(False, "--print"),
) -> None:
    if interval <= 0:
        raise typer.BadParameter("interval must be positive")
    content = agent_plist(interval)
    if print_only:
        typer.echo(content.decode())
        return
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    state.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if LAUNCH_AGENT_PATH.exists():
        try:
            run_launchctl(["bootout", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)])
        except subprocess.CalledProcessError:
            pass
    LAUNCH_AGENT_PATH.write_bytes(content)
    run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)])


@costs_app.command("uninstall-agent")
def uninstall_agent() -> None:
    if LAUNCH_AGENT_PATH.exists():
        try:
            run_launchctl(["bootout", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)])
        except subprocess.CalledProcessError:
            pass
        LAUNCH_AGENT_PATH.unlink()

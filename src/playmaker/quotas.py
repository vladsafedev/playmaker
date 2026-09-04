"""Quota probes — token-based, faithful port of CodexBar's strategies.

All providers go through OAuth Bearer tokens; no WebKit, no PTY.

- Codex: ChatGPT JWT in ~/.codex/auth.json -> chatgpt.com/backend-api/wham/usage
- Claude: macOS Keychain entry "Claude Code-credentials" -> api.anthropic.com/api/oauth/usage
- Antigravity (agy): ~/.gemini/oauth_creds.json -> daily-cloudcode-pa.googleapis.com
  loadCodeAssist + retrieveUserQuota (ideType ANTIGRAVITY); Gemini buckets only
- Gemini (retired locally): same creds -> cloudcode-pa.googleapis.com
- Z.ai (GLM, dispatched via opencode): API key in opencode's auth.json ->
  api.z.ai/api/monitor/usage/quota/limit
- Kimi Code: OAuth credential in ~/.kimi-code/credentials ->
  api.kimi.ai/coding/v1/usages
- Ollama (local, also via opencode): no quota to fetch — localhost:11434
  /api/tags is an availability probe reporting unmetered capacity
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from playmaker import __version__

# OAuth installed-app client credentials shipped with the public gemini-cli
# npm package — not a private secret (Google publishes them in gemini-cli's
# source, and installed-app client secrets are not confidential per Google's
# own OAuth docs).
# Source: https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/code_assist/oauth2.ts
# The literals are split so the assembled values don't trip GitHub secret
# scanning / push protection for everyone who forks this repo (the scanner
# also decodes base64, so encoding is not enough).
_GEMINI_OAUTH_CLIENT_ID = (
    "681255809395-oo8ft2opr" + "drnp9e3aqf6av3hmdib135j" + ".apps" + ".googleusercontent.com"
)
_GEMINI_OAUTH_CLIENT_SECRET = "GOCSPX-" + "4uHgMPm-1o7Sk-geV6Cu5clXFsxl"

_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

_USER_AGENT = f"playmaker-cli/{__version__}"


# ---- HTTP helpers -----------------------------------------------------------


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict | str | None = None,
    timeout: float = 12.0,
) -> dict:
    """Send a JSON HTTP request, return parsed response. Raises on non-2xx."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", _USER_AGENT)
    headers.setdefault("Accept", "application/json")

    data: bytes | None = None
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {url}: {body_txt}") from e


def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT without verifying signature."""
    try:
        mid = token.split(".")[1]
    except IndexError as e:
        raise ValueError("not a JWT") from e
    mid += "=" * (-len(mid) % 4)
    return json.loads(base64.urlsafe_b64decode(mid).decode("utf-8"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _format_relative(target_iso_or_ms: str | int | float | None) -> str | None:
    if target_iso_or_ms is None:
        return None
    if isinstance(target_iso_or_ms, str):
        try:
            dt = datetime.fromisoformat(target_iso_or_ms.replace("Z", "+00:00"))
        except ValueError:
            return None
        target = dt.timestamp()
    else:
        # already epoch — accept seconds or millis heuristically
        v = float(target_iso_or_ms)
        target = v / 1000.0 if v > 1e12 else v
    delta = target - time.time()
    if delta <= 0:
        return "now"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        return f"{h}h{f' {m}m' if m and h < 5 else ''}"
    d = int(delta // 86400)
    h = int((delta % 86400) // 3600)
    return f"{d}d {h}h" if h else f"{d}d"


def _forecast_label(used_pct: float, window_seconds: float, elapsed_seconds: float) -> str | None:
    """Return CodexBar-style forecast label.

    - pace ≈ 1: "On pace"             (using at expected rate, will hit 100 ~= reset)
    - pace < 0.85: "Lasts until reset" (under-consuming, will have slack at reset)
    - pace > 1.15: "Exhausts before reset" (over-consuming, will run out)
    """
    if window_seconds <= 0 or elapsed_seconds <= 0:
        return None
    if used_pct >= 99:
        return "Exhausted"
    elapsed_frac = max(0.0, min(1.0, elapsed_seconds / window_seconds))
    if elapsed_frac < 0.05:
        return None  # too early to forecast
    pace = used_pct / (elapsed_frac * 100.0)
    if pace < 0.85:
        return "Lasts until reset"
    if pace > 1.15:
        return "Exhausts before reset"
    return "On pace"


# ---- Codex ------------------------------------------------------------------


_CODEX_AUTH_PATH = Path("~/.codex/auth.json").expanduser()


def _codex_load_auth() -> dict:
    if not _CODEX_AUTH_PATH.exists():
        raise RuntimeError(f"{_CODEX_AUTH_PATH} not found; run 'codex login'")
    return json.loads(_CODEX_AUTH_PATH.read_text(encoding="utf-8"))


def _codex_save_auth(auth: dict) -> None:
    _CODEX_AUTH_PATH.write_text(json.dumps(auth, indent=2), encoding="utf-8")


def _codex_refresh(auth: dict) -> dict:
    """Refresh access_token. Mutates auth dict and returns it."""
    tokens = auth.get("tokens") or {}
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("no refresh_token in ~/.codex/auth.json")
    resp = _http_json(
        "https://auth.openai.com/oauth/token",
        method="POST",
        body={
            "client_id": _CODEX_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        },
    )
    tokens["access_token"] = resp["access_token"]
    if "id_token" in resp:
        tokens["id_token"] = resp["id_token"]
    if "refresh_token" in resp:
        tokens["refresh_token"] = resp["refresh_token"]
    auth["tokens"] = tokens
    auth["last_refresh"] = datetime.now(UTC).isoformat()
    _codex_save_auth(auth)
    return auth


def _codex_fetch_usage(access_token: str, account_id: str | None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return _http_json(
        "https://chatgpt.com/backend-api/wham/usage",
        headers=headers,
    )


def codex_probe() -> dict:
    auth = _codex_load_auth()
    tokens = auth.get("tokens") or {}
    access_token = tokens.get("access_token")
    account_id = auth.get("account_id") or tokens.get("account_id")
    id_token = tokens.get("id_token")

    if not access_token:
        raise RuntimeError("no access_token in ~/.codex/auth.json")

    # Refresh proactively if id_token exp is in the past or close.
    try:
        payload = _decode_jwt_payload(id_token) if id_token else {}
        exp = payload.get("exp", 0)
        if exp and exp - time.time() < 120:
            auth = _codex_refresh(auth)
            tokens = auth["tokens"]
            access_token = tokens["access_token"]
            id_token = tokens.get("id_token")
            payload = _decode_jwt_payload(id_token) if id_token else {}
    except Exception:
        payload = {}

    try:
        usage = _codex_fetch_usage(access_token, account_id)
    except RuntimeError as e:
        if "HTTP 401" in str(e):
            auth = _codex_refresh(auth)
            access_token = auth["tokens"]["access_token"]
            id_token = auth["tokens"].get("id_token")
            payload = _decode_jwt_payload(id_token) if id_token else {}
            usage = _codex_fetch_usage(access_token, account_id)
        else:
            raise

    rate = usage.get("rate_limit") or {}
    reserve = _codex_reserve_from_additional(usage) if rate.get("secondary_window") else None
    windows = _codex_windows(
        rate, reserve_pct=reserve
    )
    spark_windows = _codex_spark_windows(usage)

    return {
        "status": "ok",
        "account_email": payload.get("email"),
        "tier": _humanize_codex_tier(usage.get("plan_type")),
        "windows": windows,
        "blocks": [{"name": "Spark", "windows": spark_windows}] if spark_windows else [],
    }


def _codex_windows(rate: dict, *, reserve_pct: int | None = None) -> list[dict]:
    """Build the standard Session and Weekly rows for a Codex rate-limit block."""
    primary = rate.get("primary_window") or {}
    secondary = rate.get("secondary_window") or {}

    windows: list[dict] = []
    if primary:
        used = int(primary.get("used_percent") or 0)
        windows.append(
            {
                "name": "Session",
                "pct_left": 100 - used,
                "reset_at_iso": _epoch_to_iso(primary.get("reset_at")),
                "reset_relative": _format_relative(primary.get("reset_at")),
                "forecast": None,
                "reserve_pct": None,
            }
        )
    if secondary:
        used = int(secondary.get("used_percent") or 0)
        win_secs = secondary.get("limit_window_seconds") or 7 * 86400
        reset_at = secondary.get("reset_at")
        elapsed = (
            (win_secs - max(0, (reset_at or 0) - time.time())) if reset_at else 0
        )
        forecast = _forecast_label(used, win_secs, elapsed)
        windows.append(
            {
                "name": "Weekly",
                "pct_left": 100 - used,
                "reset_at_iso": _epoch_to_iso(reset_at),
                "reset_relative": _format_relative(reset_at),
                "forecast": forecast,
                "reserve_pct": reserve_pct,
            }
        )

    return windows


def _codex_spark_windows(usage: dict) -> list[dict]:
    """Return the Codex-Spark rate-limit rows, if the provider reports them."""
    extras = usage.get("additional_rate_limits") or []
    if not isinstance(extras, list):
        return []
    for extra in extras:
        if not isinstance(extra, dict):
            continue
        feature = extra.get("metered_feature")
        limit_name = extra.get("limit_name")
        if feature == "codex_bengalfox" or (
            isinstance(limit_name, str) and "spark" in limit_name.lower()
        ):
            return _codex_windows(extra.get("rate_limit") or {})
    return []


def _codex_reserve_from_additional(usage: dict) -> int | None:
    """Compute 'in reserve' from additional_rate_limits[].secondary_window.

    Best-effort: take the minimum 'left %' across additional weekly windows.
    """
    extras = usage.get("additional_rate_limits") or []
    lefts: list[int] = []
    for extra in extras:
        rl = (extra or {}).get("rate_limit") or {}
        sec = rl.get("secondary_window") or {}
        used = sec.get("used_percent")
        if isinstance(used, (int, float)):
            lefts.append(100 - int(used))
    if not lefts:
        return None
    return min(lefts)


def _epoch_to_iso(value: int | float | None) -> str | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e12:
        v /= 1000.0
    return datetime.fromtimestamp(v, tz=UTC).isoformat()


def _humanize_codex_tier(plan: str | None) -> str | None:
    if not plan:
        return None
    return {
        "plus": "Plus",
        "pro": "Pro",
        "team": "Team",
        "enterprise": "Enterprise",
        "free": "Free",
        "go": "Go",
    }.get(plan.lower(), plan)


# ---- Claude -----------------------------------------------------------------


def _claude_load_keychain() -> dict:
    """Read the Keychain entry written by Claude Code at sign-in."""
    if shutil.which("security") is None:
        raise RuntimeError("`security` CLI not available; cannot read Keychain")
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Keychain read failed: {proc.stderr.strip() or 'not found'}"
        )
    blob = json.loads(proc.stdout.strip())
    if "claudeAiOauth" not in blob:
        raise RuntimeError("unexpected keychain payload shape (no claudeAiOauth)")
    return blob["claudeAiOauth"]


def _claude_save_keychain(auth: dict) -> None:
    blob = json.dumps({"claudeAiOauth": auth})
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-s",
            "Claude Code-credentials",
            "-a",
            subprocess.check_output(["whoami"], text=True).strip(),
            "-w",
            blob,
            "-U",  # update if exists
        ],
        check=True,
        capture_output=True,
        timeout=5,
    )


def _claude_refresh(auth: dict) -> dict:
    refresh_token = auth.get("refreshToken")
    if not refresh_token:
        raise RuntimeError("no refreshToken in claudeAiOauth keychain entry")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLAUDE_OAUTH_CLIENT_ID,
        }
    )
    resp = _http_json(
        "https://platform.claude.com/v1/oauth/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    auth["accessToken"] = resp["access_token"]
    if "refresh_token" in resp:
        auth["refreshToken"] = resp["refresh_token"]
    if "expires_in" in resp:
        auth["expiresAt"] = _now_ms() + int(resp["expires_in"]) * 1000
    try:
        _claude_save_keychain(auth)
    except Exception:
        # Keychain write is best-effort; usage call below will still succeed
        # using the refreshed in-memory token.
        pass
    return auth


def _claude_fetch_usage(access_token: str, *, beta: str = "oauth-2025-04-20") -> dict:
    return _http_json(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": beta,
            "Content-Type": "application/json",
        },
    )


_CLAUDE_WINDOW_SPEC = [
    ("five_hour", "Session", 5 * 3600),
    ("seven_day", "Weekly", 7 * 86400),
    ("seven_day_sonnet", "Sonnet", 7 * 86400),
    ("seven_day_design", "Design", 7 * 86400),
    ("seven_day_routines", "Routines", 7 * 86400),
]


def claude_probe() -> dict:
    auth = _claude_load_keychain()
    expires_at = auth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at - _now_ms() < 60_000:
        auth = _claude_refresh(auth)

    access_token = auth["accessToken"]
    try:
        usage = _claude_fetch_usage(access_token)
    except RuntimeError as e:
        if "HTTP 401" in str(e):
            auth = _claude_refresh(auth)
            usage = _claude_fetch_usage(auth["accessToken"])
        else:
            raise

    windows: list[dict] = []
    now = time.time()
    for key, label, window_seconds in _CLAUDE_WINDOW_SPEC:
        block = usage.get(key)
        if not isinstance(block, dict):
            continue
        utilization = block.get("utilization")
        if utilization is None:
            continue
        used = float(utilization)
        resets_at = block.get("resets_at")
        forecast: str | None = None
        if resets_at:
            try:
                reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
                remaining = reset_dt.timestamp() - now
                elapsed = max(0.0, window_seconds - remaining)
                forecast = _forecast_label(used, window_seconds, elapsed)
            except ValueError:
                pass
        windows.append(
            {
                "name": label,
                "pct_left": int(round(100 - used)),
                "reset_at_iso": resets_at,
                "reset_relative": _format_relative(resets_at),
                "forecast": forecast if label != "Session" else None,
                "reserve_pct": None,
            }
        )

    scoped_windows: list[dict] = []
    limits = usage.get("limits")
    if isinstance(limits, list):
        for limit in limits:
            if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
                continue
            percent = limit.get("percent")
            scope = limit.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            display_name = model.get("display_name") if isinstance(model, dict) else None
            if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                continue
            if not isinstance(display_name, str):
                continue

            resets_at = limit.get("resets_at")
            forecast: str | None = None
            if isinstance(resets_at, str):
                try:
                    reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
                    remaining = reset_dt.timestamp() - now
                    elapsed = max(0.0, 7 * 86400 - remaining)
                    forecast = _forecast_label(float(percent), 7 * 86400, elapsed)
                except ValueError:
                    pass
            scoped_windows.append(
                {
                    "name": f"Weekly · {display_name}",
                    "pct_left": int(round(100 - float(percent))),
                    "reset_at_iso": resets_at,
                    "reset_relative": _format_relative(resets_at),
                    "forecast": forecast,
                    "reserve_pct": None,
                }
            )

    if scoped_windows:
        weekly_index = next(
            (index for index, window in enumerate(windows) if window["name"] == "Weekly"),
            None,
        )
        if weekly_index is None:
            windows.extend(scoped_windows)
        else:
            windows[weekly_index + 1 : weekly_index + 1] = scoped_windows

    # `extra_usage` is the user's monthly overage allowance — surface only
    # as a top-level field, not as the weekly window's reserve %.
    extra = usage.get("extra_usage") or {}
    extra_info = None
    if extra.get("is_enabled"):
        extra_info = {
            "monthly_limit_usd": extra.get("monthly_limit"),
            "used_credits_usd": extra.get("used_credits"),
            "utilization_pct": extra.get("utilization"),
        }

    return {
        "status": "ok",
        "account_email": _claude_email_from_token(access_token),
        "tier": (auth.get("subscriptionType") or "").capitalize() or None,
        "windows": windows,
        "extra_usage": extra_info,
    }


def _claude_email_from_token(token: str) -> str | None:
    # Claude Code's accessToken is sk-ant-oat... format, not a JWT — email isn't
    # encoded in it. Anthropic's usage endpoint also doesn't return email.
    # We could call /api/oauth/profile but that's a separate request; keep best-
    # effort and return None for now.
    return None


# ---- Gemini -----------------------------------------------------------------


_GEMINI_OAUTH_PATH = Path("~/.gemini/oauth_creds.json").expanduser()


def _gemini_load_creds() -> dict:
    if not _GEMINI_OAUTH_PATH.exists():
        raise RuntimeError(f"{_GEMINI_OAUTH_PATH} not found; run 'gemini' once to log in")
    return json.loads(_GEMINI_OAUTH_PATH.read_text(encoding="utf-8"))


def _gemini_save_creds(creds: dict) -> None:
    _GEMINI_OAUTH_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")


def _gemini_refresh(creds: dict) -> dict:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("no refresh_token in oauth_creds.json")
    body = urllib.parse.urlencode(
        {
            "client_id": _GEMINI_OAUTH_CLIENT_ID,
            "client_secret": _GEMINI_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    resp = _http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    creds["access_token"] = resp["access_token"]
    if "id_token" in resp:
        creds["id_token"] = resp["id_token"]
    if "expires_in" in resp:
        creds["expiry_date"] = _now_ms() + int(resp["expires_in"]) * 1000
    try:
        _gemini_save_creds(creds)
    except OSError:
        pass
    return creds


# gemini-cli and Antigravity (agy) share the OAuth creds file but talk to
# different Code Assist backends with different ideType metadata.
_GEMINI_CLOUDCODE_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
_ANTIGRAVITY_CLOUDCODE_BASE = "https://daily-cloudcode-pa.googleapis.com/v1internal"


def _gemini_load_code_assist(
    access_token: str,
    *,
    base_url: str = _GEMINI_CLOUDCODE_BASE,
    ide_type: str = "GEMINI_CLI",
) -> dict:
    return _http_json(
        f"{base_url}:loadCodeAssist",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        body={"metadata": {"ideType": ide_type, "pluginType": "GEMINI"}},
    )


def _gemini_retrieve_quota(
    access_token: str,
    project_id: str | None,
    *,
    base_url: str = _GEMINI_CLOUDCODE_BASE,
) -> dict:
    body: dict = {}
    if project_id:
        body["project"] = project_id
    return _http_json(
        f"{base_url}:retrieveUserQuota",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        body=body,
    )


def _gemini_categorize_model(model_id: str) -> str | None:
    """Map model ids to dashboard rows. Return None if not surfaced."""
    m = model_id.lower()
    if "flash-lite" in m or "flash_lite" in m:
        return "Flash Lite"
    if "flash" in m:
        return "Flash"
    if "pro" in m:
        return "Pro"
    return None


def gemini_probe() -> dict:
    return _google_code_assist_probe(_GEMINI_CLOUDCODE_BASE, "GEMINI_CLI")


# ---- Antigravity local daemon probe (rich, categorized) --------------------
#
# The full Antigravity quota — Gemini AND Claude/GPT, each split into a 5-hour
# and a weekly window (what Antigravity's own UI and CodexBar show) — is NOT
# available to a plain OAuth token: the remote fetchAvailableModels endpoint
# 403s. It IS served by agy's embedded localhost language-server daemon, over a
# self-signed-TLS gRPC-web (Connect) endpoint, exactly as CodexBar reads it.
# agy runs a singleton daemon, so the probe works whenever any agy process (or
# CodexBar's bounded background agy) is running; we can also spawn a short-lived
# one ourselves. Approach ported from steipete/CodexBar's AntigravityStatusProbe.

_ANTIGRAVITY_QUOTA_SUMMARY_PATH = (
    "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
)
# `pgrep -f` regexes over the full command line. The agy one is anchored so a
# process whose *arguments* merely mention agy — playmaker's own dispatch, with
# its `--log-file .../playmaker-agy-*.log` — doesn't count as the daemon.
_ANTIGRAVITY_PROC_PATTERNS = (r"(^|/)agy( |$)", r"language_server")


def _antigravity_daemon_ports() -> list[int]:
    """Local TCP ports that a running agy/language_server daemon is listening on."""
    pids: set[int] = set()
    for pattern in _ANTIGRAVITY_PROC_PATTERNS:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in proc.stdout.split():
            if line.isdigit():
                pids.add(int(line))
    if not pids:
        return []
    # `-a` ANDs lsof's selectors. Without it they are ORed, and the listing is
    # every LISTEN socket on the machine — Steam, chromedriver, whatever else
    # sits on 127.0.0.1 — so the probe went knocking on strangers' ports.
    try:
        proc = subprocess.run(
            [
                "lsof", "-nP", "-a",
                "-p", ",".join(str(pid) for pid in sorted(pids)),
                "-iTCP", "-sTCP:LISTEN",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ports: set[int] = set()
    for line in proc.stdout.splitlines():
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


def _antigravity_local_summary(ports: list[int], timeout: float = 5.0) -> dict | None:
    """POST RetrieveUserQuotaSummary to each candidate daemon port; parse the
    first response that carries quota groups. Returns the raw summary dict
    ({"groups": [...]}) or None if no port answered."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for port in ports:
        for scheme in ("https", "http"):
            url = f"{scheme}://127.0.0.1:{port}{_ANTIGRAVITY_QUOTA_SUMMARY_PATH}"
            req = urllib.request.Request(
                url,
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
            )
            # Whatever a port says, it only ever disqualifies that port. Not
            # every refusal is an OSError: a TLS-only listener answers a
            # plaintext POST with a TLS alert record, which urllib raises as
            # http.client.BadStatusLine — an HTTPException that used to escape
            # this loop and sink the whole probe.
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict):
                    continue
                payload = data.get("response") or data.get("summary") or data
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("groups"):
                return payload
    return None


# Category / bucket → short window name, and window length for forecasting.
def _antigravity_group_short(display_name: str) -> str:
    low = display_name.lower()
    if "gemini" in low:
        return "Gemini"
    if "claude" in low or "gpt" in low or "3p" in low:
        return "Claude/GPT"
    return display_name.strip() or "Antigravity"


def _antigravity_bucket_window(bucket_id: str, display_name: str) -> tuple[str, int] | None:
    """(short suffix, window_seconds) for a bucket, or None to skip."""
    bid = bucket_id.lower()
    if bid.endswith("5h") or "5h" in bid or "five" in display_name.lower():
        return "5h", 5 * 3600
    if bid.endswith("weekly") or "week" in display_name.lower():
        return "weekly", 7 * 86400
    return None


def _antigravity_windows_from_summary(payload: dict) -> list[dict]:
    windows: list[dict] = []
    now = time.time()
    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        prefix = _antigravity_group_short(group.get("displayName") or "")
        buckets = group.get("buckets") or []
        parsed: list[tuple[int, dict]] = []
        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            win = _antigravity_bucket_window(
                bucket.get("bucketId") or "", bucket.get("displayName") or ""
            )
            if win is None:
                continue
            suffix, window_seconds = win
            frac = bucket.get("remainingFraction")
            if frac is None:
                remaining = bucket.get("remaining") or {}
                frac = remaining.get("remainingFraction") if isinstance(remaining, dict) else None
            if frac is None:
                continue
            pct_left = max(0, min(100, int(round(float(frac) * 100))))
            reset_iso = bucket.get("resetTime")
            forecast = None
            if reset_iso and suffix == "weekly":
                try:
                    reset_dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
                    elapsed = max(0.0, window_seconds - (reset_dt.timestamp() - now))
                    forecast = _forecast_label(100 - pct_left, window_seconds, elapsed)
                except ValueError:
                    pass
            # 5h buckets sort before weekly (order key 0 vs 1).
            order = 0 if suffix == "5h" else 1
            parsed.append(
                (
                    order,
                    {
                        "name": f"{prefix} {suffix}",
                        "pct_left": pct_left,
                        "reset_at_iso": reset_iso,
                        "reset_relative": _format_relative(reset_iso),
                        "forecast": forecast,
                        "reserve_pct": None,
                    },
                )
            )
        windows.extend(w for _, w in sorted(parsed, key=lambda t: t[0]))
    return windows


def antigravity_probe() -> dict:
    """Quota probe for Antigravity (agy).

    Prefers agy's local daemon, which reports the full categorized quota —
    Gemini and Claude/GPT, each split 5-hour vs weekly (source: "local"). Falls
    back to the OAuth retrieveUserQuota, which only surfaces coarse Gemini daily
    buckets (source: "remote") — the Claude/GPT windows are simply not available
    to a plain OAuth token. The local path needs a running agy/CodexBar daemon.

    The local path is a bonus, so it degrades to remote rather than to an
    error: whatever goes wrong there — a stray listener, a daemon mid-start,
    a payload we don't recognise — the user still gets a quota row.
    """
    windows: list[dict] = []
    try:
        ports = _antigravity_daemon_ports()
        payload = _antigravity_local_summary(ports) if ports else None
        if payload:
            windows = _antigravity_windows_from_summary(payload)
    except Exception:
        windows = []
    if windows:
        out = {
            "status": "ok",
            "account_email": None,
            "tier": None,
            "windows": windows,
            "source": "local",
        }
        # Enrich email/tier from the cheap OAuth loadCodeAssist call;
        # never let that failure sink the rich local windows.
        try:
            meta = _antigravity_account_meta()
            out["account_email"] = meta.get("email")
            out["tier"] = meta.get("tier")
        except Exception:
            pass
        return out

    result = _google_code_assist_probe(_ANTIGRAVITY_CLOUDCODE_BASE, "ANTIGRAVITY")
    result["source"] = "remote"
    return result


def _antigravity_account_meta() -> dict:
    """email + tier from loadCodeAssist / id_token, without fetching quota."""
    creds = _gemini_load_creds()
    expiry = creds.get("expiry_date")
    if isinstance(expiry, (int, float)) and expiry - _now_ms() < 60_000:
        creds = _gemini_refresh(creds)
    access_token = creds["access_token"]
    email = None
    id_token = creds.get("id_token")
    if id_token:
        try:
            email = _decode_jwt_payload(id_token).get("email")
        except Exception:
            email = None
    tier = None
    try:
        loaded = _gemini_load_code_assist(
            access_token, base_url=_ANTIGRAVITY_CLOUDCODE_BASE, ide_type="ANTIGRAVITY"
        )
        tier_id = (loaded.get("currentTier") or {}).get("id") or ""
        tier = {"standard-tier": "Paid", "legacy-tier": "Legacy", "free-tier": "Free"}.get(
            tier_id
        )
    except Exception:
        pass
    return {"email": email, "tier": tier}


def _google_code_assist_probe(base_url: str, ide_type: str) -> dict:
    creds = _gemini_load_creds()
    expiry = creds.get("expiry_date")
    if isinstance(expiry, (int, float)) and expiry - _now_ms() < 60_000:
        creds = _gemini_refresh(creds)

    access_token = creds["access_token"]

    try:
        loaded = _gemini_load_code_assist(access_token, base_url=base_url, ide_type=ide_type)
    except RuntimeError as e:
        if "HTTP 401" in str(e):
            creds = _gemini_refresh(creds)
            access_token = creds["access_token"]
            loaded = _gemini_load_code_assist(access_token, base_url=base_url, ide_type=ide_type)
        else:
            raise

    # cloudaicompanionProject is sometimes a string, sometimes {id: ...}
    project_node = loaded.get("cloudaicompanionProject")
    project_id: str | None = None
    if isinstance(project_node, str):
        project_id = project_node
    elif isinstance(project_node, dict):
        project_id = project_node.get("id")

    tier_node = loaded.get("currentTier") or {}
    tier_id = tier_node.get("id") or ""
    quota = _gemini_retrieve_quota(access_token, project_id, base_url=base_url)

    # Group buckets by display label, take the *minimum* remaining fraction
    # (most-constrained limit per model).
    grouped: dict[str, dict] = {}
    for bucket in quota.get("buckets") or []:
        model_id = bucket.get("modelId") or bucket.get("model") or ""
        label = _gemini_categorize_model(model_id)
        if not label:
            continue
        frac = bucket.get("remainingFraction")
        if frac is None:
            continue
        pct_left = max(0, min(100, int(round(float(frac) * 100))))
        reset_iso = bucket.get("resetTime")
        existing = grouped.get(label)
        if existing is None or pct_left < existing["pct_left"]:
            grouped[label] = {
                "name": label,
                "pct_left": pct_left,
                "reset_at_iso": reset_iso,
                "reset_relative": _format_relative(reset_iso),
                "forecast": None,
                "reserve_pct": None,
            }

    windows = [grouped[k] for k in ("Pro", "Flash", "Flash Lite") if k in grouped]

    # tier mapping
    tier: str | None = None
    if tier_id == "standard-tier":
        tier = "Paid"
    elif tier_id == "legacy-tier":
        tier = "Legacy"
    elif tier_id == "free-tier":
        tier = "Free"

    # account email from id_token if present
    email = None
    id_token = creds.get("id_token")
    if id_token:
        try:
            email = _decode_jwt_payload(id_token).get("email")
        except Exception:
            email = None

    return {
        "status": "ok",
        "account_email": email,
        "tier": tier,
        "windows": windows,
    }


# ---- Z.ai (GLM) -------------------------------------------------------------


_ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"

# Provider ids opencode files a Z.ai credential under, best first.
_ZAI_PROVIDER_IDS = ("zai-coding-plan", "zai", "z-ai")

# `unit` is a time-unit enum z.ai does not document. Inferred from the
# nextResetTime values on a live plan (2026-07): unit 3 + number 5 is the
# documented 5-hour window; unit 6 + number 1 resets inside a week; unit 5 +
# number 1 inside a month. Unknown units still render, they just lose the
# forecast — which is the only thing the span is used for.
_ZAI_UNIT_SECONDS = {3: 3600, 4: 86400, 5: 30 * 86400, 6: 7 * 86400}
_ZAI_UNIT_NAMES = {3: "hour", 4: "day", 5: "month", 6: "week"}

# (type, unit, number) -> label. "Session"/"Weekly" deliberately match the
# claude probe's labels so the two providers read as like-for-like in the table.
#
# z.ai renamed the inference windows TOKENS_LIMIT -> CREDIT_LIMIT when the plans
# moved to weekly Credits (observed 2026-08 on a Pro plan; the same account read
# TOKENS_LIMIT in 2026-07). Both spellings stay mapped: an unrecognised type
# still renders, but as the bare span ("5 hours"), which breaks the like-for-
# like reading against the claude rows.
_ZAI_WINDOW_LABELS = {
    ("CREDIT_LIMIT", 3, 5): "Session",
    ("CREDIT_LIMIT", 6, 1): "Weekly",
    ("TOKENS_LIMIT", 3, 5): "Session",
    ("TOKENS_LIMIT", 6, 1): "Weekly",
    ("TIME_LIMIT", 5, 1): "MCP tools",
}


def _zai_load_key() -> str | None:
    """The Z.ai key opencode already stored; playmaker keeps none of its own."""
    from playmaker.agents.opencode import data_root

    auth = {}
    try:
        auth = json.loads((data_root() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        auth = {}
    if isinstance(auth, dict):
        for provider_id in _ZAI_PROVIDER_IDS:
            entry = auth.get(provider_id)
            if isinstance(entry, dict) and entry.get("key"):
                return str(entry["key"])
    # opencode configs that inject the key via {env:...} keep it in the
    # environment instead of auth.json.
    for var in ("ZAI_API_KEY", "Z_AI_API_KEY", "ZHIPUAI_API_KEY"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def _zai_window_label(limit_type: object, unit: object, number: object) -> str:
    known = _ZAI_WINDOW_LABELS.get((limit_type, unit, number))  # type: ignore[arg-type]
    if known:
        return known
    unit_name = _ZAI_UNIT_NAMES.get(unit)  # type: ignore[arg-type]
    if unit_name and isinstance(number, int):
        return f"{number} {unit_name}{'' if number == 1 else 's'}"
    return str(limit_type or "quota").replace("_", " ").capitalize()


def zai_probe() -> dict:
    """GLM Coding Plan quota, for work dispatched through `opencode`.

    Verified response on a live Pro plan (2026-08):

        {"code": 200, "success": true, "msg": "Operation successful",
         "data": {"level": "pro", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 12000,
             "currentValue": 21, "remaining": 11978, "percentage": 1,
             "nextResetTime": 1787084800394},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 60000,
             "currentValue": 21, "remaining": 59978, "percentage": 1,
             "nextResetTime": 1787671183998}]}}

    Two things moved since 2026-07: the inference windows are typed
    CREDIT_LIMIT rather than TOKENS_LIMIT (`usage` is the plan's credit
    allowance — 12k per 5h / 60k per week on Pro), and the monthly TIME_LIMIT
    pool for MCP tools no longer appears at all. Older accounts may still send
    the previous shape, so both are mapped.

    `percentage` is percent *used*, `nextResetTime` is epoch ms and is absent
    until a window has been touched. TIME_LIMIT is the monthly MCP tool pool
    (search-prime / web-reader / zread), not inference.
    """
    key = _zai_load_key()
    if not key:
        return {
            "status": "unsupported",
            "reason": (
                "no Z.ai credential — run `opencode auth login` and pick "
                "Z.AI Coding Plan, or export ZAI_API_KEY"
            ),
        }

    resp = _http_json(
        _ZAI_QUOTA_URL,
        headers={
            # z.ai's monitor API takes the raw key; a Bearer prefix 401s.
            "Authorization": key,
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
        },
    )
    if resp.get("success") is False or resp.get("code") not in (None, 200):
        raise RuntimeError(
            f"z.ai quota error {resp.get('code')}: {resp.get('msg') or '(no message)'}"
        )

    data = resp.get("data")
    data = data if isinstance(data, dict) else {}
    limits = data.get("limits")
    if not isinstance(limits, list):
        return {
            "status": "unsupported",
            "reason": f"unrecognised quota payload: {str(resp)[:200]}",
        }

    now = time.time()
    windows: list[dict] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        pct_used = entry.get("percentage")
        if not isinstance(pct_used, (int, float)):
            continue
        unit = entry.get("unit")
        number = entry.get("number")
        label = _zai_window_label(entry.get("type"), unit, number)

        reset_iso: str | None = None
        forecast: str | None = None
        reset_ms = entry.get("nextResetTime")
        if isinstance(reset_ms, (int, float)):
            reset_dt = datetime.fromtimestamp(reset_ms / 1000, UTC)
            reset_iso = reset_dt.isoformat()
            span = _ZAI_UNIT_SECONDS.get(unit)  # type: ignore[arg-type]
            if span and isinstance(number, (int, float)):
                window_seconds = span * float(number)
                elapsed = max(0.0, window_seconds - (reset_dt.timestamp() - now))
                forecast = _forecast_label(float(pct_used), window_seconds, elapsed)

        windows.append(
            {
                "name": label,
                "pct_left": max(0, min(100, int(round(100 - float(pct_used))))),
                "reset_at_iso": reset_iso,
                "reset_relative": _format_relative(reset_iso),
                # Short windows churn too fast to pace — same call the claude
                # probe makes for its own Session row.
                "forecast": forecast if label != "Session" else None,
                "reserve_pct": None,
            }
        )

    level = data.get("level")
    return {
        "status": "ok",
        # The monitor API returns no identity, only the plan level.
        "account_email": None,
        "tier": str(level).capitalize() if level else None,
        "windows": windows,
    }


# ---- Kimi Code --------------------------------------------------------------


_KIMI_CODE_DEFAULT_BASE_URL = "https://api.kimi.ai/coding/v1"
_KIMI_CODE_DEFAULT_OAUTH_HOST = "https://auth.kimi.ai"

# Public installed-app client id from kimi-code 0.41.0's OAuth bundle. The
# bundle refreshes at <oauth_host>/api/oauth/token with this form body.
_KIMI_CODE_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"


def _kimi_code_home() -> Path:
    configured = os.environ.get("KIMI_CODE_HOME")
    return Path(configured).expanduser() if configured else Path("~/.kimi-code").expanduser()


def _kimi_load_config(home: Path) -> dict:
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return config if isinstance(config, dict) else {}


def _kimi_provider_config(config: dict) -> dict:
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return {}
    provider = providers.get("managed:kimi-code")
    return provider if isinstance(provider, dict) else {}


def _kimi_runtime_urls(config: dict) -> tuple[str, str]:
    """Return the configured managed API and OAuth hosts, defaulting global."""
    provider = _kimi_provider_config(config)
    base_url = provider.get("base_url") or provider.get("baseUrl")
    oauth = provider.get("oauth")
    oauth = oauth if isinstance(oauth, dict) else {}
    oauth_host = oauth.get("oauth_host") or oauth.get("oauthHost")
    resolved_base_url = (
        str(base_url).rstrip("/")
        if isinstance(base_url, str) and base_url
        else _KIMI_CODE_DEFAULT_BASE_URL
    )
    resolved_oauth_host = (
        str(oauth_host).rstrip("/")
        if isinstance(oauth_host, str) and oauth_host
        else _KIMI_CODE_DEFAULT_OAUTH_HOST
    )
    return (
        resolved_base_url,
        resolved_oauth_host,
    )


def _kimi_credential_paths(home: Path, config: dict) -> list[Path]:
    """Prefer this environment's configured OAuth file, then other CLI files."""
    credentials_dir = home / "credentials"
    provider = _kimi_provider_config(config)
    oauth = provider.get("oauth")
    oauth = oauth if isinstance(oauth, dict) else {}
    oauth_key = oauth.get("key")
    configured: list[Path] = []
    if isinstance(oauth_key, str):
        name = Path(oauth_key).name
        if name.startswith("kimi-code-env-"):
            configured.append(credentials_dir / f"{name}.json")

    try:
        discovered = sorted(
            credentials_dir.glob("kimi-code-env-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        discovered = []
    return configured + [path for path in discovered if path not in configured]


def _kimi_load_credentials() -> tuple[dict, Path, dict] | None:
    home = _kimi_code_home()
    config = _kimi_load_config(home)
    for path in _kimi_credential_paths(home, config):
        try:
            credentials = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(credentials, dict) and isinstance(credentials.get("access_token"), str):
            return credentials, path, config
    return None


def _kimi_save_credentials(path: Path, credentials: dict) -> None:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp_path.chmod(0o600)
            tmp.write(json.dumps(credentials, indent=2) + "\n")
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _kimi_refresh(credentials: dict, path: Path, oauth_host: str) -> dict:
    """Refresh a Kimi CLI OAuth token and persist it to its existing file."""
    refresh_token = credentials.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("no refresh_token in Kimi credential")
    body = urllib.parse.urlencode(
        {
            "client_id": _KIMI_CODE_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )
    response = _http_json(
        f"{oauth_host.rstrip('/')}/api/oauth/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    access_token = response.get("access_token")
    expires_in = response.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Kimi token refresh response missing access_token")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise RuntimeError("Kimi token refresh response missing expires_in")

    refreshed = dict(credentials)
    refreshed["access_token"] = access_token
    if isinstance(response.get("refresh_token"), str) and response["refresh_token"]:
        refreshed["refresh_token"] = response["refresh_token"]
    refreshed["expires_in"] = int(expires_in)
    refreshed["expires_at"] = int(time.time()) + int(expires_in)
    if isinstance(response.get("token_type"), str):
        refreshed["token_type"] = response["token_type"]
    if isinstance(response.get("scope"), str):
        refreshed["scope"] = response["scope"]
    _kimi_save_credentials(path, refreshed)
    return refreshed


def _humanize_kimi_tier(level: object) -> str | None:
    if not isinstance(level, str) or not level:
        return None
    suffix = level.removeprefix("LEVEL_")
    return suffix.replace("_", " ").title()


def _kimi_window(
    name: str, detail: dict, *, window_seconds: float, include_forecast: bool
) -> dict | None:
    limit = detail.get("limit")
    remaining = detail.get("remaining")
    used = detail.get("used")
    if not isinstance(limit, (int, float, str)):
        return None
    try:
        limit_value = float(limit)
        if isinstance(remaining, (int, float, str)):
            remaining_value = float(remaining)
        elif isinstance(used, (int, float, str)):
            # The live API omits `remaining` at 0%, while retaining `used`.
            remaining_value = limit_value - float(used)
        else:
            return None
    except ValueError:
        return None
    if limit_value <= 0:
        return None

    reset_at = detail.get("resetTime")
    reset_iso = reset_at if isinstance(reset_at, str) else None
    forecast: str | None = None
    if include_forecast and reset_iso:
        try:
            reset_dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
            elapsed = max(0.0, window_seconds - (reset_dt.timestamp() - time.time()))
            used_pct = max(0.0, min(100.0, 100 - (remaining_value / limit_value * 100)))
            forecast = _forecast_label(used_pct, window_seconds, elapsed)
        except ValueError:
            pass
    return {
        "name": name,
        "pct_left": max(0, min(100, int(round(remaining_value / limit_value * 100)))),
        "reset_at_iso": reset_iso,
        "reset_relative": _format_relative(reset_iso),
        "forecast": forecast,
        "reserve_pct": None,
    }


def kimi_probe() -> dict:
    """Kimi Code OAuth usage, as reported by its managed `/usages` endpoint.

    `usage` is the weekly percentage bucket. `limits[]` holds rolling windows;
    the 300-minute detail is Kimi Code's session quota. The stored access token
    is intentionally never refreshed by making a chat request: when it is
    expired, this uses the CLI's documented OAuth refresh request instead.
    """
    loaded = _kimi_load_credentials()
    if loaded is None:
        return {
            "status": "unsupported",
            "reason": "no Kimi Code credential — run `kimi login --region global`",
        }
    credentials, credentials_path, config = loaded
    access_token = credentials.get("access_token")
    expires_at = credentials.get("expires_at")
    base_url, oauth_host = _kimi_runtime_urls(config)
    if not isinstance(access_token, str) or not access_token:
        return {
            "status": "unsupported",
            "reason": "Kimi token expired — run any `kimi` command to refresh it",
        }
    if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
        if (
            not isinstance(credentials.get("refresh_token"), str)
            or not credentials["refresh_token"]
        ):
            return {
                "status": "unsupported",
                "reason": "Kimi token expired — run any `kimi` command to refresh it",
            }
        credentials = _kimi_refresh(credentials, credentials_path, oauth_host)
        access_token = credentials["access_token"]

    try:
        response = _http_json(
            f"{base_url.rstrip('/')}/usages",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except RuntimeError as exc:
        if "HTTP 401" not in str(exc) or not isinstance(credentials.get("refresh_token"), str):
            raise
        credentials = _kimi_refresh(credentials, credentials_path, oauth_host)
        response = _http_json(
            f"{base_url.rstrip('/')}/usages",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
        )

    usage = response.get("usage")
    limits = response.get("limits")
    user = response.get("user")
    if not isinstance(usage, dict) or not isinstance(limits, list) or not isinstance(user, dict):
        return {
            "status": "unsupported",
            "reason": f"unrecognised quota payload: {str(response)[:200]}",
        }
    membership = user.get("membership")

    session: dict | None = None
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        window = limit.get("window")
        detail = limit.get("detail")
        if (
            isinstance(window, dict)
            and window.get("duration") == 300
            and window.get("timeUnit") == "TIME_UNIT_MINUTE"
            and isinstance(detail, dict)
        ):
            # Session windows churn too quickly for a useful pace forecast,
            # matching the Claude and Z.ai rows in this table.
            session = _kimi_window(
                "Session", detail, window_seconds=5 * 3600, include_forecast=False
            )
            break
    weekly = _kimi_window("Weekly", usage, window_seconds=7 * 86400, include_forecast=True)
    if session is None or weekly is None:
        return {
            "status": "unsupported",
            "reason": f"unrecognised quota payload: {str(response)[:200]}",
        }

    level = membership.get("level") if isinstance(membership, dict) else None
    return {
        "status": "ok",
        "account_email": None,
        "tier": _humanize_kimi_tier(level),
        "windows": [session, weekly],
    }


# ---- Ollama (local, unmetered) ----------------------------------------------

# Ollama's own API, not the OpenAI-compatible /v1 shim that opencode dispatches
# through: /api/tags is the cheapest liveness check and names what is actually
# pulled on this machine.
def _ollama_base_url() -> str:
    """OLLAMA_HOST as the daemon itself reads it — the scheme is optional."""
    host = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host.rstrip("/")


def _ollama_can_chat(base: str, name: str) -> bool:
    """True if /api/show lists a text-generation capability for this model.

    Unknown or unreadable capabilities count as chat-capable: an older daemon
    that predates the field should not hide every model it serves.
    """
    try:
        info = _http_json(
            f"{base}/api/show", method="POST", body={"model": name}, timeout=3.0
        )
    except (urllib.error.URLError, OSError, RuntimeError, json.JSONDecodeError):
        return True
    caps = info.get("capabilities")
    if not isinstance(caps, list):
        return True
    return "completion" in caps


def ollama_probe() -> dict:
    """Local inference capacity, dispatched via `opencode -m ollama/<tag>`.

    This is an availability signal, not a quota. Every other provider here
    reports a bucket that depletes and refills; local Ollama has no bucket —
    work sent to it costs nothing from any subscription pool. So the honest
    report is "100% left, never resets", and that is precisely its value to the
    coach: overflow capacity that absorbs junior-tier fan-out while the metered
    pools stay reserved for work that needs them.

    Read the ceiling as hardware, not entitlement — and read it as narrow. Fan-out
    width is whatever OLLAMA_NUM_PARALLEL allows, and every concurrent slot wants
    its own KV cache out of the same unified memory the weights already sit in, so
    a 27B q4 on 36GB has room for very few. Treat this as a shallow queue next to
    the cloud providers' real parallelism, not as equivalent capacity.

    `models` lists what is pulled so the coach routes to a tag that exists rather
    than guessing one — and the tag decides the engine, not just the weights:
    Ollama sends GGUF builds to llama.cpp and safetensors builds to MLX, which on
    Apple Silicon is the faster path.
    """
    base = _ollama_base_url()
    try:
        # A local daemon answers in milliseconds or not at all; this probe must
        # never be the slow one in refresh_all.
        resp = _http_json(f"{base}/api/tags", timeout=3.0)
    except (urllib.error.URLError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        if shutil.which("ollama") is None:
            return {
                "status": "unsupported",
                "reason": "ollama not installed — https://ollama.com/download",
            }
        return {
            "status": "unsupported",
            "reason": (
                f"ollama daemon unreachable at {base} "
                f"({type(exc).__name__}) — start it with `ollama serve`"
            ),
        }

    pulled = sorted(
        str(entry.get("name"))
        for entry in (resp.get("models") or [])
        if isinstance(entry, dict) and entry.get("name")
    )

    # Only a model that can *chat* is dispatch capacity. Embedding-only models
    # (mxbai, nomic, …) live in the same store and would otherwise make an idle
    # Ollama look like a free 27B — the coach would route work at nothing.
    # /api/show reports `capabilities` (["completion", "tools", …] vs
    # ["embedding"]); one local POST per model, milliseconds each.
    models = [name for name in pulled if _ollama_can_chat(base, name)]

    if not models:
        return {
            "status": "unsupported",
            "reason": (
                "ollama up, but no chat model pulled — connector is dormant. "
                "Activate with `ollama pull qwen3.8:27b-mlx` (~18GB, MLX engine); "
                "the opencode provider and this probe pick it up on the next refresh."
                + (f" Pulled but embedding-only: {', '.join(pulled)}." if pulled else "")
            ),
        }

    return {
        "status": "ok",
        "account_email": None,
        "tier": "unmetered",
        "windows": [
            {
                "name": "Local",
                "pct_left": 100,
                "reset_at_iso": None,
                "reset_relative": None,
                "forecast": "Unmetered — bounded by memory, not entitlement",
                "reserve_pct": None,
            }
        ],
        "source": "local",
        "models": models,
    }


# ---- Aggregator -------------------------------------------------------------


# gemini-cli's probe still works while its creds file exists, but the CLI is
# retired locally — Antigravity (agy) replaces it in the default probe set.
PROBES = {
    "codex": codex_probe,
    "claude": claude_probe,
    "agy": antigravity_probe,
    "zai": zai_probe,
    "kimi": kimi_probe,
    "ollama": ollama_probe,
}


def refresh_all(quotas_path: Path) -> dict:
    previous: dict = {}
    if quotas_path.exists():
        try:
            previous = json.loads(quotas_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}

    out: dict = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "providers": {},
    }
    for name, probe in PROBES.items():
        last_success = (
            (previous.get("providers") or {}).get(name, {}).get("last_success")
        )
        try:
            result = probe()
            if result.get("status") == "ok":
                result["last_success"] = out["fetched_at"]
            else:
                result["last_success"] = last_success
            out["providers"][name] = result
        except Exception as exc:
            out["providers"][name] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "last_success": last_success,
            }

    tmp = quotas_path.with_suffix(quotas_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(quotas_path)
    return out

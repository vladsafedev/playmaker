"""Quota probes — token-based, faithful port of CodexBar's strategies.

All three providers go through OAuth Bearer tokens; no WebKit, no PTY.

- Codex: ChatGPT JWT in ~/.codex/auth.json -> chatgpt.com/backend-api/wham/usage
- Claude: macOS Keychain entry "Claude Code-credentials" -> api.anthropic.com/api/oauth/usage
- Gemini: ~/.gemini/oauth_creds.json -> cloudcode-pa.googleapis.com loadCodeAssist + retrieveUserQuota
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# OAuth client credentials shipped with the public gemini-cli npm package.
# Source: https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/code_assist/oauth2.ts
_GEMINI_OAUTH_CLIENT_ID = "REDACTED-public-value-see-current-quotas.py"
_GEMINI_OAUTH_CLIENT_SECRET = "REDACTED-public-value-see-current-quotas.py"

_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

_USER_AGENT = "playmaker-cli/0.1"


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
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat()
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
        # "In reserve" — averaged unused weekly across the additional metered
        # rate-limits (CodexBar surfaces this so users see they have headroom
        # in alternative buckets like Codex-Spark).
        reserve = _codex_reserve_from_additional(usage)
        windows.append(
            {
                "name": "Weekly",
                "pct_left": 100 - used,
                "reset_at_iso": _epoch_to_iso(reset_at),
                "reset_relative": _format_relative(reset_at),
                "forecast": forecast,
                "reserve_pct": reserve,
            }
        )

    return {
        "status": "ok",
        "account_email": payload.get("email"),
        "tier": _humanize_codex_tier(usage.get("plan_type")),
        "windows": windows,
    }


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
    return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()


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


def _gemini_load_code_assist(access_token: str) -> dict:
    return _http_json(
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        method="POST",
        headers={"Authorization": f"Bearer {access_token}"},
        body={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
    )


def _gemini_retrieve_quota(access_token: str, project_id: str | None) -> dict:
    body: dict = {}
    if project_id:
        body["project"] = project_id
    return _http_json(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
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
    creds = _gemini_load_creds()
    expiry = creds.get("expiry_date")
    if isinstance(expiry, (int, float)) and expiry - _now_ms() < 60_000:
        creds = _gemini_refresh(creds)

    access_token = creds["access_token"]

    try:
        loaded = _gemini_load_code_assist(access_token)
    except RuntimeError as e:
        if "HTTP 401" in str(e):
            creds = _gemini_refresh(creds)
            access_token = creds["access_token"]
            loaded = _gemini_load_code_assist(access_token)
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
    quota = _gemini_retrieve_quota(access_token, project_id)

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


# ---- Aggregator -------------------------------------------------------------


PROBES = {
    "codex": codex_probe,
    "claude": claude_probe,
    "gemini": gemini_probe,
}


def refresh_all(quotas_path: Path) -> dict:
    previous: dict = {}
    if quotas_path.exists():
        try:
            previous = json.loads(quotas_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}

    out: dict = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
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

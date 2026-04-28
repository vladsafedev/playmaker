"""Static playmaker capabilities for ACP `initialize`.

Phase 1 had us forward Zed's initialize to a child and rewrite the response
(corr-6). Phase 2 pivot: we are no longer a transparent proxy. We are a
proper agent — for `session/load` we serve from state.db without a child
at all, and for `session/new` we own the session lifecycle.

Static caps means initialize is fast (no spawn on Zed startup) and
predictable. Trade-off: if we declare a capability we do not actually
support yet, we'll hit it at runtime.

The shape mirrors claude-acp's response (we observed it in
~/acp-logs/claude-20260428-224329.out.jsonl line 1) so Zed UI features
that depend on these caps continue to work. Values were chosen to match
what playmaker can actually deliver in Phase 2:

  - loadSession = true               we serve session/load from state.db
  - sessionCapabilities.close        we handle session/close (kill child)
  - promptCapabilities.image=true    children we wrap support images
  - mcpCapabilities.http=true        children support http MCP
"""

from __future__ import annotations

from typing import Any


PROXY_NAME = "playmaker"
PROXY_TITLE = "Playmaker"
PROXY_VERSION = "0.1.0"


def initialize_response(zed_id: int) -> dict[str, Any]:
    """Build a JSON-RPC reply to Zed's `initialize` request.

    Static — does not require a live child.

    `authMethods` is REQUIRED by Zed UI even though playmaker itself doesn't
    auth — sub-agents handle their own authentication (Claude subscription,
    OpenAI key, Google account). Without at least one declared method, Zed
    renders "Failed to Launch — Authentication required" on sidebar click,
    even when session/load flow works fine. Declare a single passthrough
    method so Zed considers the agent ready.
    """
    return {
        "jsonrpc": "2.0",
        "id": zed_id,
        "result": {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "image": True,
                    "embeddedContext": True,
                },
                "mcpCapabilities": {"http": True, "sse": True},
                "sessionCapabilities": {"close": {}},
            },
            "agentInfo": {
                "name": PROXY_NAME,
                "title": PROXY_TITLE,
                "version": PROXY_VERSION,
            },
            "authMethods": [
                {
                    "id": "playmaker-passthrough",
                    "name": "Pre-authenticated (sub-agents handle auth)",
                    "description": (
                        "playmaker proxies threads whose underlying agents "
                        "(Claude, Codex, Gemini) are already authenticated. "
                        "No login flow needed here."
                    ),
                },
            ],
        },
    }


def init_error_response(zed_id: int, message: str) -> dict[str, Any]:
    """JSON-RPC error reply to initialize. Currently unused (static caps
    cannot fail) — kept for symmetry with Phase 1 corr-16 plus future
    Phase 3 where we might want to fail-fast on missing dependencies.
    """
    return {
        "jsonrpc": "2.0",
        "id": zed_id,
        "error": {
            "code": -32000,
            "message": f"playmaker: {message}",
        },
    }

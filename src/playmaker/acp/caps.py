"""Protocol constants and capability-rewriting helpers.

(corr-6) We do NOT hardcode an INITIAL_CAPS bag. Instead we eagerly spawn
the child on Zed's `initialize`, forward Zed's request verbatim, and
take child's response — rewriting only the agent-identifying fields.
"""

from __future__ import annotations

from typing import Any


PROXY_NAME = "playmaker"
PROXY_TITLE = "Playmaker (Claude proxy)"


def rewrite_init_response(child_response: dict[str, Any]) -> dict[str, Any]:
    """Rewrite child's initialize response so Zed sees us as the agent.

    (corr-6) Capabilities, _meta, version, authMethods — pass through.
    Only agentInfo.{name, title} are overridden so Zed's identity layer
    reflects that it's talking to playmaker, not directly to the child.
    """
    out = dict(child_response)
    result = dict(out.get("result") or {})
    agent_info = dict(result.get("agentInfo") or {})
    agent_info["name"] = PROXY_NAME
    agent_info["title"] = PROXY_TITLE
    result["agentInfo"] = agent_info
    out["result"] = result
    return out


def init_error_response(zed_id: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error reply to Zed's initialize (corr-16).

    Used when child spawn or child's own initialize fails — without this,
    Zed would hang or show an unhelpful generic failure.
    """
    return {
        "jsonrpc": "2.0",
        "id": zed_id,
        "error": {
            "code": -32000,
            "message": f"playmaker: child failed to initialize: {message}",
        },
    }

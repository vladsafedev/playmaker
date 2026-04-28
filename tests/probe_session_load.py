"""Minimal ACP server probe: logs every incoming JSON-RPC frame to /tmp/acp_probe.log.

Goal: answer one question — does Zed send `session/load` to its agent_server
when the user clicks a sidebar_threads row with this agent_id, after the row
was inserted into Zed's DB by something other than Plus-menu (in our case,
playmaker dispatch).

Behavior:
- On `initialize`: declare loadSession=true, respond.
- On `session/load`: respond with empty result so Zed doesn't hang.
- On anything else with an id: respond with empty result.
- All inputs/outputs logged to /tmp/acp_probe.log.

Register in Zed settings.json `agent_servers` as `session-load-probe`.
"""

import datetime
import json
import sys

LOG_PATH = "/tmp/acp_probe.log"


def log(direction: str, payload) -> None:
    ts = datetime.datetime.now().isoformat()
    line = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    with open(LOG_PATH, "a") as fh:
        fh.write(f"[{ts}] {direction}: {line}\n")


def respond(msg: dict) -> None:
    out = json.dumps(msg, ensure_ascii=False)
    log("OUT", out)
    sys.stdout.write(out + "\n")
    sys.stdout.flush()


def main() -> None:
    log("---", "probe started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        log("IN", line)
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            log("ERR", f"json decode: {exc}")
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            respond({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {
                            "image": False,
                            "embeddedContext": False,
                        },
                        "sessionCapabilities": {"close": {}},
                    },
                    "agentInfo": {
                        "name": "session-load-probe",
                        "title": "Session Load Probe",
                        "version": "0.0.1",
                    },
                },
            })
            continue

        if method == "session/load":
            log("===", "GOT session/load — probe answer is YES, path B is alive")
            respond({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            continue

        if method == "session/new":
            respond({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"sessionId": "probe-session-fresh"},
            })
            continue

        if msg_id is not None:
            # Stub answer for anything else so Zed doesn't error out.
            respond({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            continue

        # Notification — just log.

    log("---", "stdin EOF, exiting")


if __name__ == "__main__":
    main()

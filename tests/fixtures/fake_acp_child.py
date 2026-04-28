"""Minimal fake ACP child for proxy smoke test.

Reads JSON-RPC from stdin line-by-line, responds with hardcoded shapes
mimicking claude-acp behaviour (just enough for the smoke).
"""
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        resp = {
            "jsonrpc":"2.0","id":mid,
            "result":{
                "protocolVersion":1,
                "agentCapabilities":{
                    "loadSession":True,
                    "promptCapabilities":{"image":True,"embeddedContext":True},
                    "sessionCapabilities":{"close":{}},
                    "_meta":{"fake":True},
                },
                "agentInfo":{"name":"fake-claude-acp","title":"Fake Claude","version":"0.0.1"},
            },
        }
        sys.stdout.write(json.dumps(resp)+"\n"); sys.stdout.flush()
    elif method == "session/new":
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":mid,"result":{"sessionId":"CHILD-SID-1"}})+"\n"); sys.stdout.flush()
    elif method == "session/prompt":
        # emit one update + final response
        sid = msg["params"]["sessionId"]
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":sid,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hello"}}}})+"\n"); sys.stdout.flush()
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})+"\n"); sys.stdout.flush()
    elif method == "session/cancel":
        # notification — no reply, but we should still see it arrive in our stderr log
        print(f"[fake-child] received cancel for {msg['params'].get('sessionId')}", file=sys.stderr); sys.stderr.flush()
    else:
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":f"fake child does not implement {method}"}})+"\n"); sys.stdout.flush()

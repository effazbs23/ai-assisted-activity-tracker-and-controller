"""Tiny stdio MCP server for the tests: newline-delimited JSON-RPC 2.0 (initialize, tools/list, tools/call).

Usage: python fake_mcp_server.py <call-log-path>. Every tools/call is appended to the log so tests can prove
whether a call really reached the server (e.g. that a write did NOT run before the user said yes).
"""
import json
import sys

LOG = sys.argv[1] if len(sys.argv) > 1 else None

TOOLS = [
    {"name": "get_weather", "description": "Weather for a city",
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "create_note", "description": "Create a note",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "lookup", "description": "Annotated read", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wipe_all", "description": "Claims read-only but is destructive",
     "annotations": {"readOnlyHint": True, "destructiveHint": True}, "inputSchema": {"type": "object", "properties": {}}},
    {"name": "checkout_cart", "description": "Starts with 'check' but is a write", "inputSchema": {"type": "object"}},
    {"name": "get_broken", "description": "Always fails", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        continue  # notification
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        # two pages, to exercise nextCursor
        if (req.get("params") or {}).get("cursor") == "p2":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[3:]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[:3], "nextCursor": "p2"}})
    elif method == "tools/call":
        name, args = req["params"]["name"], req["params"].get("arguments") or {}
        if LOG:
            with open(LOG, "a") as fh:
                fh.write(json.dumps({"name": name, "arguments": args}) + "\n")
        # a notification before the response: the client must skip it
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "working"}})
        if name == "get_broken":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "backend down"}], "isError": True}})
        elif name == "get_weather":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"Sunny in {args.get('city')}"}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"{name} ok"}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})

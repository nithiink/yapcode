#!/usr/bin/env python3
"""A real MCP server over stdio, for tests. Misbehaves on demand.

Written by hand rather than taken from the SDK, for two reasons: the SDK's
in-process test helper does not exist in mcp 2.x (checked), and a fake we own
can break in the specific ways that need testing. Behaviour is chosen by
argv[1]:

    ok          two tools; call returns text
    empty       initialises but advertises no tools
    error       its tool answers with isError: true
    hang        never answers tools/call
    die         exits during tools/call, after writing to stderr
    noise       prints junk to stdout before each valid reply
    badexit     writes to stderr and exits before the handshake
    destructive advertises a tool hinting destructiveHint: true
    flood       advertises 30 tools
    deaf        reads stdin and never replies, so the handshake times out
    collide     two tool names that slug to the same registered name
"""
import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"


def send(obj):
    if MODE == "noise":
        sys.stdout.write("this is not json at all\n")
        sys.stdout.flush()
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def tools():
    if MODE == "empty":
        return []
    if MODE == "destructive":
        return [{"name": "wipe", "description": "Delete everything.",
                 "inputSchema": {"type": "object", "properties": {}},
                 "annotations": {"destructiveHint": True}}]
    if MODE == "collide":
        # Two upstream names that slug to one registered name.
        return [{"name": n, "description": f"Named {n}.",
                 "inputSchema": {"type": "object", "properties": {}}}
                for n in ("no_args", "no-args")]
    if MODE == "flood":
        return [{"name": f"tool_{i}", "description": f"Tool number {i}.",
                 "inputSchema": {"type": "object", "properties": {}}} for i in range(30)]
    return [
        {"name": "echo", "description": "Echo the text back. A second sentence.",
         "inputSchema": {"type": "object",
                         "properties": {"text": {"type": "string", "description": "What to echo."}},
                         "required": ["text"]},
         "annotations": {"readOnlyHint": True}},
        {"name": "no_args", "description": "Takes nothing.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


def main():
    if MODE == "badexit":
        sys.stderr.write("fatal: MISSING_API_KEY is not set\n")
        sys.stderr.flush()
        return 1
    for line in sys.stdin:
        if MODE == "deaf":
            continue        # reads, never answers: the connect-timeout case
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue                                    # a notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "9.9.9"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools()}})
        elif method == "tools/call":
            if MODE == "hang":
                time.sleep(300)
            if MODE == "die":
                sys.stderr.write("fatal: ran out of everything\n")
                sys.stderr.flush()
                return 2
            name = (msg.get("params") or {}).get("name")
            args = (msg.get("params") or {}).get("arguments") or {}
            if MODE == "error":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "the thing went wrong"}],
                    "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{name}: {args.get('text', '')}"}],
                    "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"no such method: {method}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())

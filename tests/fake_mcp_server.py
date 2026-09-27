"""Fake MCP stdio server for tests: JSON-RPC per line, no external deps.

Behaviors via env:
- FAKE_MCP_BEHAVIOR=exit_after_list -> exit after answering tools/list
  (simulates a server dying mid-session).
"""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "echo back the text argument",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "always fails",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _respond(msg_id, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    behavior = os.environ.get("FAKE_MCP_BEHAVIOR", "")
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        if method == "initialize":
            _respond(
                req["id"],
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0.1"},
                },
            )
        elif method == "tools/list":
            _respond(req["id"], {"tools": TOOLS})
            if behavior == "exit_after_list":
                return  # server dies after listing
        elif method == "tools/call":
            params = req.get("params", {})
            if params.get("name") == "fail":
                _respond(
                    req["id"],
                    {
                        "content": [{"type": "text", "text": "boom"}],
                        "isError": True,
                    },
                )
            else:
                text = str((params.get("arguments") or {}).get("text", ""))
                _respond(
                    req["id"],
                    {"content": [{"type": "text", "text": f"echo: {text}"}], "isError": False},
                )
        elif method == "ping":
            _respond(req["id"], {})
        else:
            # notifications (no id) and unknown methods: notifications/initialized
            if "id" in req:
                sys.stdout.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "Method not found"}}
                    )
                    + "\n"
                )
                sys.stdout.flush()


if __name__ == "__main__":
    main()

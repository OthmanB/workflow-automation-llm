#!/usr/bin/env python3
"""Deterministic stdio JSON-RPC MCP fixture server for dispatcher compatibility tests.

Exposes exactly two tools:

- ``fixture_echo``: returns the provided text argument.
- ``fixture_probe``: returns a marker; used to prove exact tool deny rules.

Capture provenance: this file is paired with the pinned OpenCode 1.18.18
compatibility fixtures under tests/fixtures/opencode/1.18.18/.
"""

from __future__ import annotations

import json
import sys


def _respond(identifier, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _tools_list():
    return {
        "tools": [
            {
                "name": "echo",
                "description": "Return the provided text unchanged.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            {
                "name": "probe",
                "description": "Return a fixed probe marker.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
    }


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        request = json.loads(line)
        method = request.get("method")
        identifier = request.get("id")
        if method == "initialize":
            _respond(
                identifier,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture-mcp", "version": "1.0.0"},
                },
            )
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _respond(identifier, _tools_list())
        elif method == "tools/call":
            name = request["params"]["name"]
            arguments = request["params"].get("arguments", {})
            if name == "echo":
                text = arguments.get("text", "")
                _respond(
                    identifier,
                    {
                        "content": [{"type": "text", "text": f"echo:{text}"}],
                        "isError": False,
                    },
                )
            elif name == "probe":
                _respond(
                    identifier,
                    {
                        "content": [{"type": "text", "text": "probe-marker"}],
                        "isError": False,
                    },
                )
            else:
                _respond(identifier, error={"code": -32601, "message": f"unknown tool {name}"})
        else:
            _respond(identifier, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()

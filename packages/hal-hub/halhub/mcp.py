"""MCP server (stdio, JSON-RPC 2.0) exposing the ecosystem to Claude Code and other MCP clients.

Lists three meta-tools (find, call, modules) plus any tools pinned in
~/.config/hal/config.toml [hub].pinned, so a client's context holds a handful
of schemas rather than every tool of every module. Stdlib only.

    "hal-hub": {"command": "hal-hub", "args": ["mcp"]}
"""
from __future__ import annotations

import json
import sys

import halhub
from .registry import load_config

PROTOCOL = "2025-06-18"

META = [
    {"name": "hal_find_tools",
     "description": "Search Hal's tools (calendar, tasks, memory, system, files, desktop, media, lights, "
                    "web, mail, jobs and more) for ones that can do what the user asked. Returns names, "
                    "descriptions and parameters; then run one with hal_call.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "what you want to do, in plain words"},
         "limit": {"type": "integer", "description": "most tools to return (default 8)"}},
         "required": ["query"]}},
    {"name": "hal_call",
     "description": "Run one of Hal's tools by name with its arguments. A result with needs_confirmation "
                    "means ask the user first, then call again with arguments._confirmed = true.",
     "inputSchema": {"type": "object", "properties": {
         "tool": {"type": "string", "description": "tool name from hal_find_tools"},
         "arguments": {"type": "object", "description": "the tool's arguments"}},
         "required": ["tool"]}},
    {"name": "hal_modules",
     "description": "List the installed Hal modules and their tool names.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _pinned() -> list[dict]:
    names = load_config().get("hub", {}).get("pinned", [])
    out = []
    for name in names:
        t = halhub.get(name)
        if t is not None:
            out.append({"name": t.name, "description": t.description, "inputSchema": t.schema()})
    return out


def _text(data) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}],
            "isError": isinstance(data, dict) and "error" in data}


def handle(msg: dict) -> dict | None:
    """Answers one JSON-RPC request; returns None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "hal-hub", "version": "0.1.0"},
                  "instructions": "Hal's tools for this computer and its owner. Search with hal_find_tools "
                                  "before saying something cannot be done."}
    elif method == "tools/list":
        result = {"tools": META + _pinned()}
    elif method == "tools/call":
        params = msg.get("params", {})
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "hal_find_tools":
            from .tools import find_tools
            data = find_tools(args.get("query", ""), args.get("limit", 8))
        elif name == "hal_call":
            data = halhub.call(args.get("tool", ""), args.get("arguments") or {})
        elif name == "hal_modules":
            from .tools import modules
            data = modules()
        else:
            data = halhub.call(name, args)
        result = _text(data)
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception as e:  # noqa: BLE001 - a bad request must not end the session
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, default=str) + "\n")
            sys.stdout.flush()

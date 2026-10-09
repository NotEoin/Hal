"""The hub's own tools: searching for tools and listing modules.

Declared in hal-hub's hal-module.toml like any other module, so a chat model
can discover the rest of the ecosystem one search at a time.
"""
from __future__ import annotations

import halhub


def find_tools(query: str, limit: int = 8) -> list[dict]:
    """Returns the tools that best match a request, with their parameters."""
    return [t.summary() for t in halhub.find(query, int(limit))]


def modules() -> list[dict]:
    """Returns each installed module with its tool names."""
    from .registry import modules as installed
    return [{"module": m.name, "summary": m.summary, "path": str(m.root),
             "tools": [t.name for t in m.tools]} for m in installed()]


def call_tool(tool: str, arguments: dict | None = None) -> dict:
    """Runs any tool by name; used by clients that only see the hub's tools."""
    return halhub.call(tool, arguments or {})

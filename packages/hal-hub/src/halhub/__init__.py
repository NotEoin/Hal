"""hal-hub: one way in to every Hal module's tools.

    import halhub
    halhub.find("add a dentist appointment on thursday")   # -> [Tool, ...]
    halhub.call("calendar_add", {"title": "Dentist", "when": "thursday 10am"})
    halhub.ollama_tools("turn the lights red")               # function specs for a chat model
"""
from __future__ import annotations

import os
import ssl

from . import registry, runner, search
from .registry import Tool

# Tools run in-process inside the caller's Python; a relocated virtualenv can point OpenSSL at a
# certificate bundle that no longer exists, which breaks every HTTPS tool. Fall back to the system's.
_SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
if (not os.environ.get("SSL_CERT_FILE") and not os.path.exists(ssl.get_default_verify_paths().openssl_cafile)
        and os.path.exists(_SYSTEM_CA)):
    os.environ["SSL_CERT_FILE"] = _SYSTEM_CA

__version__ = "0.1.0.dev0"

__all__ = ["Tool", "call", "find", "get", "ollama_tools", "all_tools", "CORE"]

# Offered to a chat model on every turn whatever the request, so it can always
# look further and keep its memory.
CORE = ("hal_find_tools", "memory_remember", "memory_fact", "memory_search")

_cache: dict[str, Tool] | None = None


def all_tools(refresh: bool = False) -> dict[str, Tool]:
    global _cache
    if _cache is None or refresh:
        _cache = registry.tools()
    return _cache


def get(name: str) -> Tool | None:
    return all_tools().get(name)


def find(query: str, limit: int = 8) -> list[Tool]:
    """Returns the tools most relevant to a request."""
    return search.rank(query, list(all_tools().values()), limit)


def call(name: str, args: dict | None = None) -> dict:
    """Runs a tool by name; see runner.call for the result shape."""
    tool = get(name) or all_tools(refresh=True).get(name)
    if tool is None:
        return {"error": f"no tool named {name}; use hal_find_tools to search"}
    return runner.call(tool, args)


def ollama_tools(query: str, limit: int = 8) -> list[dict]:
    """Returns function specs for the core tools plus those relevant to `query`."""
    picked = [t for t in (get(n) for n in CORE) if t is not None]
    for t in find(query, limit):
        if t not in picked:
            picked.append(t)
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.schema()}} for t in picked]

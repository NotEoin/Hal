"""hal-hub: one way in to every Hal pack's tools.

    import halhub
    halhub.find("add a dentist appointment on thursday")   # -> [ToolSpec, ...]
    halhub.function_specs("turn the lights red")           # function specs for a chat model
    halhub.client.call("calendar_add", {...}, caller="mypack")

The module-level API (04-hub.md, "Hub facade and Python API") is loaded lazily on first use,
so `import halhub` stays cheap and never imports `mcp` (REQ-HUB-62); the names resolve to
`halhub.api`, which runs everything through one process-wide `Hub`. The v1 `CORE` tuple is
gone (tools set `always_offer` instead), and so is v1's import-time `SSL_CERT_FILE` change:
the fix lives in the worker, the process that needs it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__version__ = "0.3.0"

if TYPE_CHECKING:  # the lazy names, for type checkers and editors
    from .api import all_tools, call, default_hub, find, function_specs, get, ollama_tools
    from .hub import CallResult, Hub
    from .manifest import ToolSpec

_LAZY = {
    "default_hub": "api",
    "call": "api",
    "find": "api",
    "get": "api",
    "all_tools": "api",
    "function_specs": "api",
    "ollama_tools": "api",
    "Hub": "hub",
    "CallResult": "hub",
    "ToolSpec": "manifest",
}

__all__ = [
    "CallResult",
    "Hub",
    "ToolSpec",
    "all_tools",
    "call",
    "default_hub",
    "find",
    "function_specs",
    "get",
    "ollama_tools",
]


def __getattr__(name: str) -> object:
    """Imports the module that holds `name` on first use."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'halhub' has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))

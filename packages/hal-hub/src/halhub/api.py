"""The process-wide hub behind the module-level `halhub` API and `halhub.client` (04-hub.md, "Hub facade").

Design decisions:

- One `Hub` per process, created on first use and closed at exit. `halhub.client` shares it,
  so a pack daemon that also uses the module-level API runs one set of workers.
- Sessions are per kind of caller: `kind = "hub"`, `origin = cli` by default, or
  `kind = "routine"`, `origin = routine` for a routine run (04-hub open question 1). None has
  an approver until 01's control socket lands, so an `ask` is denied with rule `no-approver`.
- Listings (`find`, `get`, `all_tools`, `function_specs`) use the default session, so they
  leave out exactly the tools that session's calls would deny (REQ-HUB-23).
- `call` and `ollama_tools` are deprecated and each warns once per process (REQ-HUB-61).
  `call` keeps v1's dict keys (`result`, or `error`, plus `status`) so old callers keep
  working while packs move to `halhub.client.call`; `needs_confirmation` is never produced.
"""

from __future__ import annotations

import atexit
import threading
import warnings
from collections.abc import Mapping

from .gatewaychat import hub_kwargs
from .hub import FrontEnd, Hub
from .manifest import ToolSpec
from .session import HubSession

__all__ = [
    "all_tools",
    "call",
    "default_hub",
    "find",
    "function_specs",
    "get",
    "ollama_tools",
    "reset",
    "run",
    "session",
]

_lock = threading.Lock()
_hub: Hub | None = None
_sessions: dict[str, HubSession] = {}
_warned: set[str] = set()


def default_hub() -> Hub:
    """The process-wide hub, created on first use and closed at exit."""
    global _hub
    with _lock:
        if _hub is None:
            _hub = Hub(**hub_kwargs())
            atexit.register(_hub.close)
        return _hub


def session(*, routine: bool = False, unattended: bool = False) -> HubSession:
    """The process-wide session for one kind of caller, opened on first use."""
    key = f"{'routine' if routine else 'hub'}:{'unattended' if unattended else 'attended'}"
    hub = default_hub()
    with _lock:
        if key not in _sessions:
            kind, origin = ("routine", "routine") if routine else ("hub", "cli")
            _sessions[key] = hub.open_session(kind=kind, origin=origin, approver=None)
        return _sessions[key]


def reset() -> None:
    """Closes the process hub and forgets its sessions (for tests and for a process reloading config)."""
    global _hub
    with _lock:
        hub, _hub = _hub, None
        sessions = list(_sessions.values())
        _sessions.clear()
    for open_session in sessions:
        open_session.close()
    if hub is not None:
        hub.close()


def run(name: str, args: Mapping[str, object] | None = None, *, front_end: FrontEnd = "python") -> dict:
    """Calls `name` in the default session and returns `CallResult.to_dict()`; never warns."""
    return default_hub().call(name, args, session(), front_end=front_end).to_dict()


def call(name: str, args: Mapping[str, object] | None = None) -> dict:
    """Deprecated: runs a tool and returns v1's dict form; use `halhub.client.call`."""
    _deprecated("call", "halhub.client.call")
    return run(name, args)


def find(query: str, limit: int = 8) -> list[ToolSpec]:
    """The tools most relevant to a request that the default session may use."""
    return default_hub().find(query, limit, session=session())


def get(name: str) -> ToolSpec | None:
    """The tool called `name` if the default session may use it."""
    return default_hub().get(name, session())


def all_tools(refresh: bool = False) -> dict[str, ToolSpec]:
    """Every tool the default session may use, by name; `refresh` rescans the installed modules first."""
    hub = default_hub()
    if refresh:
        hub.refresh(force=True)
    return {t.name: t for t in hub.tools(session())}


def function_specs(query: str, limit: int = 8) -> list[dict]:
    """OpenAI-style function definitions for a request (see `Hub.function_specs`)."""
    return default_hub().function_specs(query, limit, session=session())


def ollama_tools(query: str, limit: int = 8) -> list[dict]:
    """Deprecated alias of `function_specs`."""
    _deprecated("ollama_tools", "halhub.function_specs")
    return function_specs(query, limit)


def _deprecated(name: str, instead: str) -> None:
    with _lock:
        if name in _warned:
            return
        _warned.add(name)
    # stacklevel 3: the warning points at the caller of the deprecated function
    warnings.warn(f"halhub.{name} is deprecated; use {instead}", DeprecationWarning, stacklevel=3)

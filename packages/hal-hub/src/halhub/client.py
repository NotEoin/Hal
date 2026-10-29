"""`halhub.client.call`: how a pack calls another pack's tool (RESOLUTIONS C9, REQ-HUB-73).

Inside a worker (`HAL_HUB_WORKER=1`) the call goes over the worker's nested-call channel, so
the hub that runs the parent call decides it with the parent's session: only tools in the
module's `calls`, only on a plain `allow`, one level deep. Anywhere else (a pack daemon such
as the routines scheduler or a reminders unit) it goes through one process-local `Hub`.

Design decisions:

- Every deny raises `ToolDenied`, including an `ask` with no approver or grant; every other
  failure raises `ToolFailed`. A pack never has to read a status dict, and an `ask` can never
  look like success.
- `caller` must equal the worker's `HAL_MODULE` inside a worker: a pack cannot claim to be
  another module (the hub checks `calls` against the module that owns the worker anyway).
- Outside a worker the session is `kind = "routine"`, `origin = routine` when `routine` is
  given, else `kind = "hub"`, `origin = cli` (04-hub open question 1). Its approver will be
  01's router with the `control` and `notify` surfaces; until 01's control socket lands
  there is no approver, so an `ask` raises `ToolDenied` with rule `no-approver` whether or
  not `unattended` is set.
- Inside a worker `timeout` is ignored: the nested call's time counts against the parent's.
"""

from __future__ import annotations

import atexit
import importlib
import os
import threading
from collections.abc import Mapping

from .hub import CallResult, Hub, RoutineRun
from .session import HubSession

__all__ = ["RoutineRun", "ToolDenied", "ToolFailed", "call"]

WORKER_ENV = "HAL_HUB_WORKER"  # set by the runner in every worker's environment
MODULE_ENV = "HAL_MODULE"

_lock = threading.Lock()
_hub: Hub | None = None
_sessions: dict[str, HubSession] = {}


class ToolDenied(Exception):
    """Policy refused the call (including an `ask` nobody could answer)."""

    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        self.message = message
        super().__init__(f"{rule}: {message}" if rule else message)


class ToolFailed(Exception):
    """The call was allowed but did not produce a result."""

    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        self.message = message
        super().__init__(f"{error_type}: {message}")


def call(
    tool: str,
    args: Mapping[str, object],
    caller: str,
    *,
    unattended: bool = False,
    routine: RoutineRun | None = None,
    timeout: float | None = None,
) -> object:
    """Runs `tool` with `args` on behalf of module `caller` and returns its result.

    Raises `ToolDenied` for any deny and `ToolFailed` for any other error; raises `ValueError`
    inside a worker when `caller` is not the worker's module.
    """
    if os.environ.get(WORKER_ENV) == "1":
        return _call_nested(tool, args, caller)
    session = _session(routine is not None, unattended)
    result = _process_hub().call(tool, args, session, front_end="python", routine=routine, timeout=timeout)
    return _unwrap(result)


def _call_nested(tool: str, args: Mapping[str, object], caller: str) -> object:
    module = os.environ.get(MODULE_ENV, "")
    if caller != module:
        raise ValueError(f"caller {caller!r} is not this worker's module {module!r}")
    worker = importlib.import_module("hal_worker")  # registered in sys.modules by the worker script
    try:
        return worker.call_tool(tool, dict(args))
    except worker.ToolCallError as exc:
        if exc.type == "policy_denied":
            raise ToolDenied(getattr(exc, "rule", ""), exc.message) from None
        raise ToolFailed(exc.type, exc.message) from None


def _unwrap(result: CallResult) -> object:
    if result.status == "ok":
        return result.result
    if result.status == "denied":
        raise ToolDenied(result.rule or "", result.message or "")
    raise ToolFailed(result.error_type or "tool_error", result.message or "")


def _process_hub() -> Hub:
    """The process-wide hub, created on first use and closed at exit."""
    global _hub
    with _lock:
        if _hub is None:
            _hub = Hub()
            atexit.register(_hub.close)
        return _hub


def _session(routine: bool, unattended: bool) -> HubSession:
    """One session per kind of caller in this process."""
    key = f"{'routine' if routine else 'hub'}:{'unattended' if unattended else 'attended'}"
    hub = _process_hub()
    with _lock:
        if key not in _sessions:
            kind, origin = ("routine", "routine") if routine else ("hub", "cli")
            _sessions[key] = hub.open_session(kind=kind, origin=origin, approver=None)
        return _sessions[key]


def reset() -> None:
    """Closes the process hub and forgets its sessions (for tests and for a pack reloading config)."""
    global _hub
    with _lock:
        hub, _hub = _hub, None
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()
    if hub is not None:
        hub.close()

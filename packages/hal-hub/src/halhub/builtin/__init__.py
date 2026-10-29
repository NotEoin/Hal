"""The hub's built-in `hub` module: where its manifest lives and the call its tools run for.

04-hub.md, "Discovery and registry" and "In-process modules". `hal_find_tools` and
`hal_modules` search and list the registry as the calling session sees it, so they need the
hub and the session of the call, but a tool function receives only its declared arguments.
`Hub` binds both in a context variable around the run (`bound`), and the runner copies the
caller's context into its in-process thread, so the tools read them with `current`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the hub imports this package; the names are for annotations only
    from ..hub import Hub
    from ..session import HubSession

__all__ = ["MANIFEST", "bound", "current"]

MANIFEST = Path(__file__).resolve().with_name("hal-module.toml")

_call: ContextVar[tuple[Hub, HubSession] | None] = ContextVar("halhub_builtin_call", default=None)


@contextmanager
def bound(hub: Hub, session: HubSession) -> Iterator[None]:
    """Makes `hub` and `session` what `current` returns until the block ends."""
    token = _call.set((hub, session))
    try:
        yield
    finally:
        _call.reset(token)


def current() -> tuple[Hub, HubSession]:
    """The hub and session of the running built-in call; raises `RuntimeError` outside one."""
    found = _call.get()
    if found is None:
        raise RuntimeError("the hub's built-in tools run only through Hub.call")
    return found

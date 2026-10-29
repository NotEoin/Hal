"""The built-in `hub` module's tools: `hal_find_tools` and `hal_modules`.

Both answer for the session that made the call, so a model never learns of a tool its session
may not use (REQ-HUB-23). They run in the hub process through `Hub.call` only (see
`halhub.builtin`).
"""

from __future__ import annotations

from . import current

__all__ = ["find_tools", "modules"]


def find_tools(query: str, limit: int = 8) -> list[dict[str, object]]:
    """The tools that best match a request, each with its tier, approval need and parameters."""
    hub, session = current()
    return [hub.describe(tool, session) for tool in hub.find(query, limit, session=session)]


def modules() -> list[dict[str, object]]:
    """Each installed module with its status and the names of the tools the session may use."""
    hub, session = current()
    return hub.modules(session)

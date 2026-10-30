"""The approval view of the TUI: shows a broker request with every argument, escaped (REQ-SEC-25, REQ-SEC-27).

The text comes from the host-made `display` of the hub, never from the model, and is escaped again
here as a second layer. The view says which tool, tier and pack, why it asks and what the options
do. An answer other than the offered letters is a deny, and so is end of input.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TextIO

from halkit import termsafe

from ..acp.permissions import PermissionBridge
from .render import safe

ANSWERS = {"o": "once", "y": "once", "s": "session", "d": "deny", "n": "deny", "": "deny"}
RULE = "-" * 60


def render_approval(request: Mapping[str, object]) -> str:
    """The approval panel text for one `approval_request` wire object."""
    scopes = request.get("scopes")
    offered = scopes if isinstance(scopes, list) else []
    choices = "[o]nce" + (" / [s]ession" if "session" in offered else "") + " / [d]eny (default)"
    lines = [
        RULE,
        f"APPROVAL  {safe(request.get('tool'))} [{safe(request.get('tier'))}] from {safe(request.get('pack'))}",
        f"why: {safe(request.get('reason', ''))}",
        "arguments (every one, as the tool will receive them):",
        termsafe.escape(str(request.get("display", "")), multiline=True),
        RULE,
        f"Allow this exact call? {choices}",
    ]
    return "\n".join(lines)


def answer_to_option(text: str, request: Mapping[str, object]) -> str:
    """Maps typed text to an ACP option id; unknown text and a `session` the request lacks are `deny`."""
    scope = ANSWERS.get(text.strip().lower(), "deny")
    scopes = request.get("scopes")
    if scope == "session" and not (isinstance(scopes, list) and "session" in scopes):
        return "deny"
    return scope


def make_asker(
    bridge: PermissionBridge, read_line: Callable[[], Awaitable[str]], out: TextIO
) -> Callable[[dict[str, object]], Awaitable[None]]:
    """A `send` for `PermissionBridge.run`: shows the request, reads one answer, feeds it back.

    End of input denies. The answer goes through `bridge.consume`, so it is bound to the request
    key like any other surface (REQ-SEC-23).
    """

    async def ask(message: dict[str, object]) -> None:
        rid = str(message["id"])
        request = bridge.request_for(rid)
        if request is None:
            return
        print(render_approval(request), file=out, flush=True)
        try:
            option = answer_to_option(await read_line(), request)
        except EOFError:
            option = "deny"
        outcome: dict[str, object] = {"outcome": "selected", "optionId": option}
        bridge.consume({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})

    return ask

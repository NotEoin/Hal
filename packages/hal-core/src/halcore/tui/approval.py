"""The approval panel of the TUI: shows a broker request with every argument, escaped (REQ-SEC-25, REQ-SEC-27).

The text comes from the host-made `display` of the hub, never from the model, and is escaped again
here as a second layer. The panel says which tool, tier and pack, the classifier's risk verdict
when the hub sent one, why it asks, the exact arguments, and what each key does. Anti-spoofing:
the panel is drawn by this module from fixed labels, model text cannot contain a line that looks
like the keys row (every control character is shown as `\\uXXXX`, and the panel border is drawn
around the whole body), the answer is read from the human's keyboard only, and anything other
than an offered letter, including end of input, is a deny. `e` denies the call and sends the
human's note to the agent as their next message.

One panel at a time: a second request waits until the first is answered. While a panel waits,
the bridge is asked every `VANISH_POLL_S` whether the request is still pending; when it was
answered elsewhere (`hal approve`, the console) the read is cancelled, a notice says so and
nothing is sent, so the next line the human types cannot answer a request they no longer see.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import TextIO

from halkit import termsafe
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..acp.permissions import PermissionBridge
from .render import safe
from .theme import Look, load_look, make_console

ANSWERS = {"o": "once", "y": "once", "a": "session", "s": "session", "d": "deny", "n": "deny", "e": "edit", "": "deny"}
RULE = "-" * 60
RISK_STYLE = {"low": "ok", "medium": "accent", "high": "bad", "critical": "bad"}
VANISH_POLL_S = 0.25  # how soon a panel notices its request was answered elsewhere
ANSWERED_ELSEWHERE = "approval answered elsewhere (or expired); this prompt is closed"


def _lines(request: Mapping[str, object]) -> list[str]:
    scopes = request.get("scopes")
    offered = scopes if isinstance(scopes, list) else []
    keys = "[y] once" + (" / [a] always this session" if "session" in offered else "") + " / [n] deny (default) / [e] deny and tell the agent why"
    lines = [f"{safe(request.get('tool'))} [{safe(request.get('tier'))}] from {safe(request.get('pack'))}"]
    risk = request.get("risk")
    if risk:
        verdict = f"classifier: {safe(risk)}"
        reason = request.get("risk_reason")
        lines.append(verdict + (f" - {safe(reason)}" if reason else ""))
    lines += [
        f"why: {safe(request.get('reason', ''))}",
        "arguments (every one, as the tool will receive them):",
        termsafe.escape(str(request.get("display", "")), multiline=True),
        "",
        f"Allow this exact call? {keys}",
    ]
    return lines


def render_approval(request: Mapping[str, object]) -> str:
    """The approval panel body as plain text for one `approval_request` wire object."""
    lines = _lines(request)
    return "\n".join([RULE, "APPROVAL  " + lines[0], *lines[1:-2], RULE, lines[-1]])


def approval_panel(request: Mapping[str, object], look: Look) -> Panel:
    """The approval request as a rounded panel; the border is the accent colour, risk is coloured."""
    pal = look.palette
    lines = _lines(request)
    body = Text()
    for i, line in enumerate(lines):
        style = ""
        if line.startswith("classifier:"):
            style = getattr(pal, RISK_STYLE.get(str(request.get("risk")), "muted"))
        elif i == len(lines) - 1:
            style = "bold"
        elif line.startswith(("why:", "arguments")):
            style = pal.muted
        body.append(line + ("\n" if i < len(lines) - 1 else ""), style=style)
    return Panel(body, title="Approval needed", title_align="left", box=look.glyphs.box, border_style=pal.accent)


def answer_to_option(text: str, request: Mapping[str, object]) -> str:
    """Maps typed text to an ACP option id; unknown text and a `session` the request lacks are `deny`.

    `e` maps to `deny`: the note is handled by the caller (see `make_asker`).
    """
    scope = ANSWERS.get(text.strip().lower(), "deny")
    scopes = request.get("scopes")
    if scope == "session" and not (isinstance(scopes, list) and "session" in scopes):
        return "deny"
    return "deny" if scope == "edit" else scope


def make_asker(
    bridge: PermissionBridge,
    read_line: Callable[[], Awaitable[str]],
    out: TextIO,
    *,
    look: Look | None = None,
    console: Console | None = None,
    suspend: Callable[[], contextlib.AbstractContextManager[None]] = contextlib.nullcontext,
    read_note: Callable[[], Awaitable[str]] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> Callable[[dict[str, object]], Awaitable[None]]:
    """A `send` for `PermissionBridge.run`: shows the panel, reads one answer, feeds it back.

    End of input denies. The answer goes through `bridge.consume`, so it is bound to the request
    key like any other surface (REQ-SEC-23). `suspend` pauses the live spinner and the Esc
    watcher while the human answers. With `e`, `read_note` asks for a note and `on_note` queues it.
    `look` is the UI's look, so the panel follows `[ui].theme`; the defaults are used without one.
    """
    look = look or load_look(stream=out)
    shown = console or make_console(look, out)
    one_at_a_time = asyncio.Lock()
    still_pending = getattr(bridge, "is_pending", lambda rid: True)

    async def answered_or_gone(rid: str, read: Callable[[], Awaitable[str]]) -> str | None:
        """The typed line, or None when the request left the bridge first (the read is cancelled)."""
        task = asyncio.ensure_future(read())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=VANISH_POLL_S)
                if done:
                    return task.result()
                if not still_pending(rid):
                    return None
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, EOFError):
                await task  # let the reader remove itself before anything else reads the terminal

    async def ask(message: dict[str, object]) -> None:
        rid = str(message["id"])
        async with one_at_a_time:
            request = bridge.request_for(rid)
            if request is None or not still_pending(rid):
                return
            with suspend():
                shown.print(approval_panel(request, look))
                option, note = "deny", ""
                try:
                    typed = await answered_or_gone(rid, read_line)
                    if typed is None:
                        shown.print(Text(ANSWERED_ELSEWHERE, style=look.palette.muted))
                        return
                    option = answer_to_option(typed, request)
                    if ANSWERS.get(typed.strip().lower()) == "edit" and read_note is not None:
                        note = (await read_note()).strip()
                except EOFError:
                    option = "deny"
        if note and on_note is not None:
            on_note(note)
        outcome: dict[str, object] = {"outcome": "selected", "optionId": option}
        bridge.consume({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})

    return ask

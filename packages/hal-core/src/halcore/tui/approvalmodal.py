"""The approval gate of the full-screen UI: one broker request in a modal, answered by the human.

The logic is the line UI's: the request's escaped parts come from `approval.view_of`, the
chosen letter goes through `approval.answer_to_option`, and only the caller's `bridge.consume`
answers, bound to the request key (REQ-SEC-23). This module only changes how the human says it.

Rules, each one tested in `test_tui_approval_modal.py`:

1. Text from the request is shown as `Text` in `SafeStatic`; the title, labels and buttons are
   fixed strings here. Request text never sets a label, id, class, tooltip or markup.
2. Deny has focus when the modal opens; Enter with nothing else done denies (REQ-CLI-27).
3. The allow buttons and keys do nothing for `ARM_S` after the modal opens, and keys sent before
   it opened are dropped: a keystroke in flight from the composer cannot allow (REQ-CLI-27).
4. Allow stays off until the arguments have been scrolled to the end, or fit (REQ-CLI-26).
5. Allowing takes two deliberate actions: `y` or `a` only moves focus to that button, then Enter
   presses it; with the mouse it is a full click on the button. Typing cannot do it: a `y` or `a`
   that follows another printable key within `TYPING_GAP_S` is ignored, and any other key while an allow
   button has focus sends focus back to Deny, so "try it" and Enter typed into the modal deny.
6. "Allow these exact arguments this session" is there only when the request offers `session`,
   and a forged `a` still goes through `answer_to_option`, which denies it.
7. Esc, Ctrl+C or the request vanishing (`gone`) close the modal as a deny or with no answer.
8. "Deny with a note" opens a line in the modal; Enter sends the note, an empty note is a deny.

Small terminals: the buttons and the note line stay on screen at 80x24 and 60x20. When the
buttons do not fit side by side they stack as one-line buttons, and the arguments view gives up
rows (down to `ARGS_MIN_ROWS`) so the rest of the box fits. The gate line keeps its row when
the arguments have been read, so the buttons never move under the pointer as they turn on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input

from . import approval
from .screens import MODAL_CSS
from .widgets import SafeStatic, toast

ARM_S = 0.6  # a person reading the panel takes longer; a key in flight from the composer arrives sooner
ALLOW_IDS = ("allow-once", "allow-session")
LETTER_OF = {"deny": "n", "deny-note": "e", "allow-once": "y", "allow-session": "a"}
RISK_CLASS = {"low": "risk-low", "medium": "risk-medium", "high": "risk-high", "critical": "risk-high"}
GONE = "gone"  # the dismiss value when the request was answered elsewhere
TYPING_GAP_S = 0.5  # keys closer together than this are a person typing a sentence, not choosing
KEEP_ALLOW_FOCUS = frozenset({"enter", "tab", "shift+tab", "y", "a", "escape", "ctrl+c"})
ARGS_MAX_ROWS = 12  # the arguments view's tallest size, border included (its CSS `max-height`)
ARGS_MIN_ROWS = 3  # one line of arguments between its borders
BOX_HEIGHT = 0.9  # `.modal-box { max-height: 90% }` in `screens.MODAL_CSS`
BOX_CHROME_ROWS = 2  # the box's top and bottom border
BUTTON_CHROME_COLS = 4  # a button's border and padding around its label, plus the gap after it
RECHECK_S = 0.05  # a wait for the next layout; a tighter `call_after_refresh` loop starves the layout
GATE_CLOSED = "scroll to read every argument before allowing (PgDn, End or the wheel)"
GATE_OPEN = "every argument shown"


class ApprovalModal(ModalScreen[tuple[str, str]]):
    """A broker approval; dismisses with `(letter, note)`: `y`, `a`, `n`, `e` or `GONE`."""

    DEFAULT_CSS = MODAL_CSS + """
    ApprovalModal { align: center middle; }
    ApprovalModal .modal-box { border: heavy $primary; width: 90%; max-width: 110; }
    ApprovalModal #args { height: auto; max-height: 12; border: round $panel; padding: 0 1; }
    ApprovalModal .risk-high { color: $error; text-style: bold; }
    ApprovalModal .risk-medium { color: $warning; }
    ApprovalModal .risk-low { color: $success; }
    ApprovalModal .why, ApprovalModal .hint, ApprovalModal .args-title { color: $text-muted; }
    ApprovalModal #gate { color: $warning; height: auto; }
    ApprovalModal #gate.open { color: $text-muted; }
    ApprovalModal #note { display: none; margin-top: 1; }
    ApprovalModal.noting #note { display: block; }
    ApprovalModal .modal-buttons { align-horizontal: left; }
    ApprovalModal .modal-buttons Button { margin: 0 1 0 0; }
    ApprovalModal.narrow .modal-buttons { layout: vertical; }
    ApprovalModal.narrow .modal-buttons Button { width: 100%; margin: 0; }
    """
    BINDINGS = [
        Binding("escape", "deny", "deny", show=False),
        Binding("ctrl+c", "deny_now", "deny", show=False, priority=True),
        Binding("n", "deny", "deny", show=False),
        Binding("e", "note", "note", show=False),
        Binding("y", "focus_allow('allow-once')", show=False),
        Binding("a", "focus_allow('allow-session')", show=False),
        Binding("pagedown", "scroll_args('page_down')", show=False),
        Binding("pageup", "scroll_args('page_up')", show=False),
        Binding("end", "scroll_args('end')", show=False),
    ]

    def __init__(self, request: Mapping[str, object], *, position: int = 1, total: int = 1, arm_s: float = ARM_S) -> None:
        super().__init__()
        self.request = request
        self.view = approval.view_of(request)
        self.position, self.total, self.arm_s = position, total, arm_s
        self.armed = False
        self.seen = False
        self.opened_at = 0.0
        self.last_key_at = 0.0

    def compose(self) -> ComposeResult:
        """Title, request facts, the arguments, the gate line, the buttons and the note line."""
        view = self.view
        with Vertical(classes="modal-box"):
            yield SafeStatic(self.title_text(), classes="modal-title")
            facts = Text(view.header, style="bold")
            if view.key:
                facts.append(f"   key {view.key}", style="dim")
            yield SafeStatic(facts)
            if view.risk:
                yield SafeStatic(Text(view.risk), classes=RISK_CLASS.get(str(self.request.get("risk")), "why"))
            yield SafeStatic(Text(view.why), classes="why")
            yield SafeStatic(Text("Arguments (every one, exactly as the tool receives them)"), classes="args-title")
            with VerticalScroll(id="args"):
                yield SafeStatic(Text(view.display))
            yield SafeStatic(Text(GATE_CLOSED), id="gate")
            yield Input(placeholder="note for the agent; Enter sends it with the deny", id="note")
            with Horizontal(classes="modal-buttons"):
                yield Button(f"{approval.DENY}  n", id="deny", variant="primary")
                yield Button(f"{approval.DENY_NOTE}  e", id="deny-note")
                yield Button(f"{approval.ALLOW_ONCE}  y", id="allow-once", disabled=True)
                if view.session_offered:
                    yield Button(f"{approval.ALLOW_SESSION}  a", id="allow-session", disabled=True)
            yield SafeStatic(Text("y or a selects an allow button; Enter or a click confirms. Enter alone denies."), classes="hint")

    def title_text(self) -> Text:
        """`Approval needed: leaves the box`, with `1 of N` when more are waiting."""
        counter = f"   {self.position} of {self.total}" if self.total > 1 else ""
        return Text(f"Approval needed: {approval.LABEL}{counter}")

    def set_total(self, total: int) -> None:
        """More requests arrived while this one is open."""
        self.total = total
        if self.is_mounted:
            self.query_one(".modal-title", SafeStatic).update(self.title_text())

    def on_mount(self, event: events.Mount) -> None:
        """Deny gets focus, the arm timer starts, and the scroll gate checks whether everything fits."""
        self.opened_at = event.time
        self.query_one("#deny", Button).focus()
        self.set_timer(self.arm_s, self.arm)
        self.call_after_refresh(self.fit)

    def on_resize(self, event: events.Resize) -> None:
        """A new terminal size: lay the box out again."""
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        """Stacks the buttons when they do not fit in a row, then sizes the arguments view."""
        if not self.is_attached:
            return
        box = self.query_one(".modal-box", Vertical)
        buttons = list(self.query(Button))
        wanted = sum(len(str(b.label)) + BUTTON_CHROME_COLS for b in buttons)
        narrow = wanted > box.content_region.width
        self.set_class(narrow, "narrow")
        for button in buttons:
            button.compact = narrow
        self.call_after_refresh(self.fit_args)

    def fit_args(self) -> None:
        """Gives the arguments the rows the rest of the box leaves, between the min and max sizes."""
        if not self.is_attached:
            return
        box = self.query_one(".modal-box", Vertical)
        args = self.query_one("#args", VerticalScroll)
        used = sum(
            child.outer_size.height + child.styles.margin.top + child.styles.margin.bottom
            for child in box.children if child is not args and child.display
        )  # fmt: skip
        room = int(self.size.height * BOX_HEIGHT) - BOX_CHROME_ROWS - used
        rows = max(ARGS_MIN_ROWS, min(ARGS_MAX_ROWS, room))
        if args.styles.max_height is None or args.styles.max_height.value != rows:
            args.styles.max_height = rows
            box.refresh(layout=True)
        self.call_after_refresh(self.check_seen)

    def arm(self) -> None:
        """The arm delay is over."""
        self.armed = True
        self.update_gate()

    def check_seen(self) -> None:
        """The arguments count as read once their view is scrolled to the end (or needs no scroll)."""
        if not self.is_attached:
            return
        args = self.query_one("#args", VerticalScroll)
        limit = args.styles.max_height
        if limit is not None and args.outer_size.height > limit.value:
            self.set_timer(RECHECK_S, self.check_seen)  # the layout predates a smaller size: judge the next one
            return
        if args.scroll_y >= args.max_scroll_y:
            self.seen = True
        self.update_gate()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """Wheel scrolling counts toward reading the arguments."""
        self.call_after_refresh(self.check_seen)

    def on_key(self, event: events.Key) -> None:
        """Drops keys sent before the modal opened and `y` / `a` inside typing; scroll keys count toward reading."""
        if event.time < self.opened_at:
            event.prevent_default()
            event.stop()
            return
        gap = event.time - self.last_key_at
        if event.is_printable:  # scroll and focus keys are not typing: End then `y` is a deliberate choice
            self.last_key_at = event.time
        if event.key in ("y", "a") and gap < TYPING_GAP_S:
            event.prevent_default()
            event.stop()
            self.query_one("#deny", Button).focus()
            return
        if isinstance(self.focused, Button) and self.focused.id in ALLOW_IDS and event.key not in KEEP_ALLOW_FOCUS:
            self.query_one("#deny", Button).focus()
        if event.key in ("down", "pagedown", "end", "space") and not self.seen:
            self.call_after_refresh(self.check_seen)

    def update_gate(self) -> None:
        """Turns the allow buttons on only when armed and every argument has been seen."""
        ready = self.armed and self.seen
        for button in self.query(Button):
            if button.id in ALLOW_IDS:
                button.disabled = not ready
        gate = self.query_one("#gate", SafeStatic)
        gate.update(Text(GATE_OPEN if self.seen else GATE_CLOSED))
        gate.set_class(self.seen, "open")

    def action_scroll_args(self, how: str) -> None:
        """PgDn, PgUp, End scroll the arguments wherever the focus is."""
        args = self.query_one("#args", VerticalScroll)
        {"page_down": args.scroll_page_down, "page_up": args.scroll_page_up, "end": args.scroll_end}[how](animate=False)
        self.call_after_refresh(self.check_seen)

    def action_focus_allow(self, button_id: str) -> None:
        """`y` or `a`: moves focus to that allow button (once it is on); it is not pressed."""
        if not (self.armed and self.seen):
            return
        found = self.query(f"#{button_id}")
        if found:
            found.first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """A pressed button answers; an allow button only when armed and seen."""
        event.stop()
        which = str(event.button.id)
        if which == "deny-note":
            self.action_note()
            return
        if which in ALLOW_IDS and not (self.armed and self.seen):
            return
        self.dismiss((LETTER_OF[which], ""))

    def action_note(self) -> None:
        """Opens the note line."""
        self.add_class("noting")
        self.query_one("#note", Input).focus()
        self.call_after_refresh(self.fit_args)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the note line: deny, with the note when there is one."""
        note = event.value.strip()
        self.dismiss(("e" if note else "n", note))

    def action_deny(self) -> None:
        """Esc or `n`: deny. Esc in the note line closes the line and goes back to Deny."""
        if self.has_class("noting"):
            self.remove_class("noting")
            self.query_one("#deny", Button).focus()
            self.call_after_refresh(self.fit_args)
            return
        self.dismiss(("n", ""))

    def action_deny_now(self) -> None:
        """Ctrl+C: deny at once, also from the note line."""
        self.dismiss(("n", ""))

    def check_consume_key(self, key: str, character: str | None) -> bool:
        """Lets the note line take every printable key (`n`, `e`, `y`, `a` included)."""
        return isinstance(self.focused, Input)

    def gone(self) -> None:
        """The request was answered elsewhere: close without an answer."""
        if self.is_attached:
            self.dismiss((GONE, ""))


class ModalAsker:
    """The full-screen UI's `send` for `PermissionBridge.run`: one modal at a time, answers via `bridge.consume`.

    Each request waits its turn (the counter says "1 of N"), the modal closes without an answer
    when the bridge says the request was answered elsewhere, and `deny_open` denies the open one
    (Ctrl+C while a turn runs).
    """

    def __init__(self, app, bridge, on_note, *, arm_s: float = ARM_S, poll_s: float = approval.VANISH_POLL_S,
                 ring: bool = True) -> None:  # noqa: ANN001 - a Textual App, a PermissionBridge and a callback
        self.app, self.bridge, self.on_note = app, bridge, on_note
        self.arm_s, self.poll_s, self.ring = arm_s, poll_s, ring
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.current: ApprovalModal | None = None
        self.tasks: set[asyncio.Future[None]] = set()

    def pending(self, rid: str) -> bool:
        """True while the bridge still waits for an answer to `rid`."""
        check = getattr(self.bridge, "is_pending", None)
        return True if check is None else bool(check(rid))

    async def send(self, message: dict[str, object]) -> None:
        """Queues one request and returns at once, so the bridge keeps polling."""
        self.waiting += 1
        if self.current is not None:
            self.current.set_total(self.waiting)
        task = asyncio.ensure_future(self.ask(str(message["id"])))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def ask(self, rid: str) -> None:
        """Shows the modal for `rid` when its turn comes and feeds the answer back."""
        try:
            async with self.lock:
                request = self.bridge.request_for(rid)
                if request is None or not self.pending(rid):
                    return
                letter, note = await self.show(rid, request)
                if letter == GONE:
                    return
                option = approval.answer_to_option(letter, request)
                if letter == "e" and note:
                    self.on_note(note)
                outcome: dict[str, object] = {"outcome": "selected", "optionId": option}
                self.bridge.consume({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})
        finally:
            self.waiting -= 1

    async def show(self, rid: str, request: Mapping[str, object]) -> tuple[str, str]:
        """Pushes the modal and waits for its answer, closing it if the request vanishes."""
        future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        modal = ApprovalModal(request, total=self.waiting, arm_s=self.arm_s)
        self.current = modal

        def done(result: tuple[str, str] | None) -> None:
            if not future.done():
                future.set_result(result or ("n", ""))

        self.app.push_screen(modal, done)
        if self.ring:
            self.app.bell()
        try:
            while not future.done():
                finished, _ = await asyncio.wait({future}, timeout=self.poll_s)
                if not finished and not self.pending(rid):
                    modal.gone()
                    toast(self.app, approval.ANSWERED_ELSEWHERE, title="Approval closed")
            return future.result()
        finally:
            self.current = None

    def deny_open(self) -> bool:
        """Denies the open modal, if any; True when there was one."""
        if self.current is not None and self.current.is_attached:
            self.current.action_deny_now()
            return True
        return False

    def close(self) -> None:
        """Drops every waiting request (the hub times them out as denies)."""
        for task in self.tasks:
            task.cancel()

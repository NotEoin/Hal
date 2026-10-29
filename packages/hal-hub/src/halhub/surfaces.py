"""Approval surfaces for the hub's own front ends (04-hub.md, "Approval surfaces implemented here").

`TtySurface` answers an `ask` on the `hal-hub` (and pack) command line: it shows the request's
host-rendered `display` and reads `y/N` from the terminal, or, with `--yes`, answers `once`
without a question. `ElicitationSurface` asks an MCP client to show the request as a form;
`halhub.mcp` supplies the transport (`elicitation/create` or `input_required`). Design
decisions:

- It offers only the `once` scope (04's surfaces table): a terminal prompt covers one call,
  never a session.
- `--yes` exists only for a person at a terminal: `yes_allowed` is false unless stdin is a
  terminal and `HAL_HARNESS` is unset, so a harness that shells out cannot use it
  (RESOLUTIONS B4, REQ-HUB-53). The request is still shown, so the person sees what `--yes`
  approved.
- The prompt goes to stderr and the answer comes from stdin; stdout stays the result's.
- It renders `display` exactly as the hub built it (already escaped), never model text.
- `in_harness` stands in for `halkit.harness.in_harness` (00-overview) until that module
  lands; it reads only `HAL_HARNESS`.
- The elicitation form offers `once` and `deny` only (01's surfaces table), defaults to
  `deny`, and any answer but `accept` with `once` is a `deny`. A request whose display is
  longer than `PREVIEW_CHARS`, or that does not offer `once`, is not for this surface
  (REQ-HUB-66): `available_for` says so, and `decide` raises `SurfaceUnavailable`, which 01's
  router counts as no answer rather than a deny. No answer at all raises `TimeoutError`, so
  silence ends as `approval-timeout` (REQ-HUB-40).
- Elicitation answers on a host harness connection are advisory (RESOLUTIONS B4, residual
  risk R-new-4): the harness runs as the user and could answer its own dialog.
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from .approvals import ApprovalRequest, Decision
from .policy.model import Scope

__all__ = [
    "ELICITATION_KEY",
    "HARNESS_ENV",
    "PREVIEW_CHARS",
    "ElicitationSurface",
    "SurfaceUnavailable",
    "TtySurface",
    "answer_decision",
    "elicitation_message",
    "elicitation_meta",
    "elicitation_schema",
    "in_harness",
    "person_at_terminal",
    "yes_allowed",
]

HARNESS_ENV = "HAL_HARNESS"  # set by `hal connect` and harness launchers (00-overview `halkit.harness`)
POLL_S = 0.2  # how often a waiting prompt checks for a withdrawal
YES_ANSWERS = frozenset({"y", "yes"})
PREVIEW_CHARS = 2000  # 04-hub REQ-HUB-66: longer requests go to surfaces that page (TUI, `hal approve`)
ELICITATION_KEY = "hal_approval"  # the `inputRequests` key on 2026-07-28 connections (04-hub)


class SurfaceUnavailable(Exception):
    """A surface was offered a request it cannot show; the router treats it as no answer."""


def in_harness(environ: Mapping[str, str] | None = None) -> bool:
    """True when `HAL_HARNESS` is set: the process was started by an AI harness."""
    env = os.environ if environ is None else environ
    return bool(env.get(HARNESS_ENV, "").strip())


def yes_allowed(stdin: TextIO | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """True when `--yes` may answer: stdin is a terminal and no harness started the process."""
    stream = sys.stdin if stdin is None else stdin
    return _isatty(stream) and not in_harness(environ)


def person_at_terminal(
    stdin: TextIO | None = None, stdout: TextIO | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """True when stdin and stdout are terminals and no harness started the process (REQ-HUB-22)."""
    return (
        _isatty(sys.stdin if stdin is None else stdin)
        and _isatty(sys.stdout if stdout is None else stdout)
        and not in_harness(environ)
    )


class TtySurface:
    """A `y/N` prompt on the controlling terminal; `yes=True` answers `once` without asking."""

    name = "tty"
    shows_all_args = True

    def __init__(
        self,
        *,
        yes: bool = False,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        prompt: TextIO | None = None,
    ) -> None:
        self.yes = yes
        self._stdin = sys.stdin if stdin is None else stdin
        self._stdout = sys.stdout if stdout is None else stdout
        self._prompt = sys.stderr if prompt is None else prompt
        self._withdrawn: set[str] = set()
        self._lock = threading.Lock()

    def available(self) -> bool:
        """True when stdin is a terminal and, for a question, stdout is one too."""
        return _isatty(self._stdin) and (self.yes or _isatty(self._stdout))

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Shows the request and returns the answer; no answer within `timeout_s` is a `deny`."""
        if Scope.ONCE not in request.scopes:
            return Decision(
                request.id, request.key, "deny", None, self.name, "the terminal answers once only"
            )
        self._write(f"\n{request.summary}\n{request.display}\n  ({request.reason})\n")
        if self.yes:
            self._write("Allowed once by --yes.\n")
            return Decision(request.id, request.key, "allow", Scope.ONCE, self.name, "--yes")
        self._write(f"Allow once? [y/N] (times out in {timeout_s:g} s) ")
        line = self._readline(request.id, time.monotonic() + timeout_s)
        if line is None:
            self._write("\n")
            return Decision(request.id, request.key, "deny", None, "timeout")
        if line.strip().lower() in YES_ANSWERS:
            return Decision(request.id, request.key, "allow", Scope.ONCE, self.name)
        return Decision(request.id, request.key, "deny", None, self.name)

    def withdraw(self, request_id: str) -> None:
        """Stops a prompt still waiting for `request_id`."""
        with self._lock:
            self._withdrawn.add(request_id)

    def _write(self, text: str) -> None:
        self._prompt.write(text)
        self._prompt.flush()

    def _readline(self, request_id: str, deadline: float) -> str | None:
        """One line from stdin before `deadline`, or None on timeout, withdrawal or end of input."""
        while True:
            with self._lock:
                if request_id in self._withdrawn:
                    return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self._stdin], [], [], min(POLL_S, remaining))
            if ready:
                line = self._stdin.readline()
                return line if line else None


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # a closed or replaced stream is not a terminal
        return False


# -- elicitation --


def elicitation_message(request: ApprovalRequest) -> str:
    """The dialog text: 01's host-rendered `display` between a heading and the approval id."""
    return (
        "Hal wants to run a tool that needs your approval.\n\n"
        f"{request.display}\n\n"
        f"Approve only if you asked for this. Approval id {request.id}."
    )


def elicitation_schema(request: ApprovalRequest) -> dict[str, Any]:
    """The form: one `decision` field, `once` or `deny`, defaulting to `deny` (04-hub)."""
    return {
        "type": "object",
        "required": ["decision"],
        "properties": {
            "decision": {
                "type": "string",
                "title": f"Run {request.tool} with these exact arguments",
                "enum": ["once", "deny"],
                "enumNames": ["Allow once", "Deny"],
                "default": "deny",
            }
        },
    }


def elicitation_meta(request: ApprovalRequest) -> dict[str, str]:
    """`_meta` for the elicitation request, with the names 01 uses on ACP."""
    return {"hal/approval_id": request.id, "hal/key": request.key}


def answer_decision(request: ApprovalRequest, answer: Mapping[str, object] | None) -> Decision:
    """The `Decision` for a client's elicitation answer: only `accept` with `once` allows."""
    content = answer.get("content") if answer is not None else None
    chosen = content.get("decision") if isinstance(content, Mapping) else None
    if answer is not None and answer.get("action") == "accept" and chosen == "once":
        return Decision(request.id, request.key, "allow", Scope.ONCE, ElicitationSurface.name)
    return Decision(request.id, request.key, "deny", None, ElicitationSurface.name)


class ElicitationSurface:
    """Asks the MCP client to show the request as a form; `send` is the connection's transport.

    `send(request, timeout_s)` blocks until the client answers and returns the answer as
    `{"action", "content"}`, or None when there was none (timeout, withdrawal, transport
    failure). `cancel(request_id)` stops a pending `send`.
    """

    name = "elicitation"
    shows_all_args = True

    def __init__(
        self,
        send: Callable[[ApprovalRequest, float], Mapping[str, object] | None],
        *,
        cancel: Callable[[str], None] | None = None,
    ) -> None:
        self._send = send
        self._cancel = cancel

    def available(self) -> bool:
        """Always true: the connection offers this surface only when the client declared forms."""
        return True

    def available_for(self, request: ApprovalRequest) -> bool:
        """True when the form can show `request`: it offers `once` and fits `PREVIEW_CHARS`."""
        return Scope.ONCE in request.scopes and len(request.display) <= PREVIEW_CHARS

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Sends the form and maps the answer.

        Raises `SurfaceUnavailable` for a request it cannot show or a dialog that failed, and
        `TimeoutError` when the client gave no answer: the router counts both as no answer
        from this surface, so another surface may still answer and silence ends as a timeout.
        """
        if not self.available_for(request):
            raise SurfaceUnavailable(f"{request.id} does not fit an elicitation form")
        try:
            answer = self._send(request, timeout_s)
        except Exception as exc:  # a failed dialog is no answer, never an allow
            raise SurfaceUnavailable(f"elicitation for {request.id} failed: {exc}") from exc
        if answer is None:
            raise TimeoutError(f"no elicitation answer for {request.id}")
        return answer_decision(request, answer)

    def withdraw(self, request_id: str) -> None:
        """Another surface answered: stop waiting for the client."""
        if self._cancel is not None:
            self._cancel(request_id)

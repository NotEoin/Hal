"""Approval surfaces for the hub's own front ends (04-hub.md, "Approval surfaces implemented here").

`TtySurface` answers an `ask` on the `hal-hub` (and pack) command line: it shows the request's
host-rendered `display` and reads `y/N` from the terminal, or, with `--yes`, answers `once`
without a question. Design decisions:

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
- The `elicitation` surface (MCP) arrives with plan step 20.
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from collections.abc import Mapping
from typing import TextIO

from ._approvals_standin import ApprovalRequest, Decision
from ._policy_standin import Scope

__all__ = ["HARNESS_ENV", "TtySurface", "in_harness", "person_at_terminal", "yes_allowed"]

HARNESS_ENV = "HAL_HARNESS"  # set by `hal connect` and harness launchers (00-overview `halkit.harness`)
POLL_S = 0.2  # how often a waiting prompt checks for a withdrawal
YES_ANSWERS = frozenset({"y", "yes"})


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

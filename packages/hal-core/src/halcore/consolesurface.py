"""The console's approval surface: queues broker requests for the page and takes its answers.

Implements `halhub.approvals.Approver`. The page lists `pending()` and answers through
`answer()`. Two rules from 06-core-cli: an answer must echo the key of the request it saw
(REQ-CLI-29), and an `allow` is refused unless the page says every argument was shown in full
(REQ-CLI-109, the console's `shows_all_args` claim). There is no "always" scope.
"""

from __future__ import annotations

import threading

from halhub.approvals import ApprovalRequest, Decision, request_json
from halhub.policy.model import Scope


class UnknownRequest(KeyError):
    """No pending request has this id (answered, expired or withdrawn)."""


class ConsoleSurface:
    """Holds pending requests until the console page answers or the timeout passes."""

    name = "console"
    shows_all_args = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[ApprovalRequest, threading.Event, list[Decision]]] = {}

    def available(self) -> bool:
        """True: the page can be opened at any time; an unanswered request times out as a deny."""
        return True

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Blocks until the page answers; no answer within `timeout_s` or a withdrawal is a deny."""
        event, answers = threading.Event(), []
        with self._lock:
            self._pending[request.id] = (request, event, answers)
        try:
            event.wait(timeout_s)
        finally:
            with self._lock:
                self._pending.pop(request.id, None)
        if answers:
            return answers[0]
        return Decision(request.id, request.key, "deny", None, "timeout")

    def withdraw(self, request_id: str) -> None:
        """Drops a request another surface answered; its waiting `decide` returns a deny."""
        with self._lock:
            entry = self._pending.get(request_id)
        if entry:
            entry[1].set()

    def pending(self) -> list[dict[str, object]]:
        """The wire form of every request still waiting, oldest first."""
        with self._lock:
            return [request_json(req) for req, _, _ in self._pending.values()]

    def answer(
        self,
        request_id: str,
        key: str,
        verdict: str,
        scope: str | None = None,
        note: str = "",
        *,
        shown_all: bool = False,
    ) -> None:
        """Records the page's answer; raises `UnknownRequest` or `ValueError` for a bad one."""
        with self._lock:
            entry = self._pending.get(request_id)
        if entry is None:
            raise UnknownRequest(request_id)
        request, event, answers = entry
        if key != request.key:
            raise ValueError("the answer's key does not match the request")
        if verdict == "deny":
            answers.append(Decision(request.id, request.key, "deny", None, self.name, note))
        elif verdict == "allow":
            if not shown_all:
                raise ValueError("every argument must be shown before an approval")
            chosen = Scope(scope) if scope in (Scope.ONCE.value, Scope.SESSION.value) else None
            if chosen is None or chosen not in request.scopes:
                raise ValueError(f"scope {scope!r} is not offered for this request")
            answers.append(Decision(request.id, request.key, "allow", chosen, self.name, note))
        else:
            raise ValueError(f"verdict must be allow or deny, got {verdict!r}")
        event.set()

"""The approval router: offers one request to every surface that may show it (01-security).

`ApprovalRouter` is itself an `Approver`, the one brokers and `hal-hub mcp` hold. It offers a
request to every available surface allowed to show it, each in its own thread; the first
answer whose `request_id` and `key` match wins, the others are withdrawn, and a mismatching
answer is dropped and audited as `approval_mismatch` (REQ-SEC-23).

Which surface may show what (REQ-SEC-30, REQ-SEC-94):

- a request with a `content` parameter never goes to a surface with `shows_all_args = false`
  (voice, desktop notification);
- such a surface gets a request only when its `display` is at most 300 characters, and only
  with scope `once`; an answer with any other scope is dropped as a mismatch;
- the `voice` surface's copy of the request carries a fresh two-digit `challenge`; no other
  surface, and never the model, sees it.

Design decisions:

- A surface's own timeout, or an exception from it, is no answer, never a `deny` that beats
  another surface; when no surface answered in time the router returns `deny` from `timeout`.
- `source` is always the answering surface's `name`, set here; a client cannot choose it.
- The router does not add grants or write the `approval` record: the hub does, because the
  grant records the tool's security digest and `ApprovalRequest` does not carry the tool
  metadata; this also audits answers from an approver that is a bare surface the same way.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from ..policy.model import Scope
from . import NOTIFY_MAX_CHARS, VOICE_MAX_CHARS, ApprovalRequest, Approver, Decision, new_challenge

if TYPE_CHECKING:  # annotation only: halhub.session imports this package
    from ..session import EventSink

log = logging.getLogger(__name__)

VOICE_SURFACE = "voice"
LIMITED_MAX_CHARS = min(VOICE_MAX_CHARS, NOTIFY_MAX_CHARS)


def _is_timeout(answer: object) -> bool:
    return isinstance(answer, TimeoutError) or (
        isinstance(answer, Decision) and answer.verdict == "deny" and answer.source == "timeout"
    )


class ApprovalRouter:
    """Offers a request to every surface that may show it; the first matching answer wins."""

    name = "router"
    shows_all_args = True

    def __init__(self, surfaces: Sequence[Approver], *, audit: EventSink | None = None) -> None:
        self.surfaces = tuple(surfaces)
        self._audit = audit

    def available(self) -> bool:
        """True when any surface is available."""
        return any(s.available() for s in self.surfaces)

    def offer(self, surface: Approver, request: ApprovalRequest) -> ApprovalRequest | None:
        """The copy of `request` that `surface` may show, or None when it may not show it."""
        if surface.shows_all_args:
            return request
        if request.content or len(request.display) > LIMITED_MAX_CHARS or Scope.ONCE not in request.scopes:
            return None
        limited = replace(request, scopes=(Scope.ONCE,))
        if surface.name == VOICE_SURFACE:
            limited = replace(limited, challenge=new_challenge())
        return limited

    def surfaces_for(self, request: ApprovalRequest) -> tuple[tuple[Approver, ApprovalRequest], ...]:
        """Available surfaces allowed to show `request`, each with the copy it would get."""
        out = []
        for surface in self.surfaces:
            if surface.available() and (offered := self.offer(surface, request)) is not None:
                out.append((surface, offered))
        return tuple(out)

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """The first answer whose id, key and scope match, else `deny` from `timeout` or `no-approver`."""
        offers = self.surfaces_for(request)
        if not offers:
            return Decision(request.id, request.key, "deny", None, "no-approver")
        answers: queue.Queue[tuple[Approver, ApprovalRequest, Decision | BaseException]] = queue.Queue()

        def ask(surface: Approver, offered: ApprovalRequest) -> None:
            try:
                answers.put((surface, offered, surface.decide(offered, timeout_s)))
            except Exception as exc:  # a broken surface counts as no answer, never as allow
                answers.put((surface, offered, exc))

        for surface, offered in offers:
            threading.Thread(
                target=ask, args=(surface, offered), name=f"approval-{surface.name}", daemon=True
            ).start()
        deadline = time.monotonic() + timeout_s
        pending = len(offers)
        timed_out = False
        winner: Decision | None = None
        while pending and winner is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                surface, offered, answer = answers.get(timeout=remaining)
            except queue.Empty:
                break
            pending -= 1
            if _is_timeout(answer):
                timed_out = True
                continue
            if isinstance(answer, BaseException):
                log.warning("approval surface %s failed: %s", surface.name, answer)
                continue
            if (
                answer.request_id != request.id
                or answer.key != request.key
                or (answer.verdict == "allow" and answer.scope not in offered.scopes)
            ):
                self._mismatch(request, answer, surface)
                continue
            winner = replace(answer, source=surface.name)
        for surface, _ in offers:
            surface.withdraw(request.id)
        if winner is not None:
            return winner
        source = "timeout" if pending or timed_out else "no-approver"
        return Decision(request.id, request.key, "deny", None, source)

    def withdraw(self, request_id: str) -> None:
        """Withdraws `request_id` from every surface (the caller gave up on it)."""
        for surface in self.surfaces:
            surface.withdraw(request_id)

    def _mismatch(self, request: ApprovalRequest, answer: Decision, surface: Approver) -> None:
        log.warning("approval %s: answer from %s did not match the request", request.id, surface.name)
        if self._audit is None:
            return
        try:
            self._audit.append(
                "approval_mismatch",
                approval=request.id,
                key_expected=request.key,
                key_got=answer.key,
                scope=str(answer.scope) if answer.scope else None,
                source=surface.name,
            )
        except OSError as exc:  # the answer is dropped either way; a lost record is logged
            log.error("approval_mismatch record for %s failed: %s", request.id, exc)

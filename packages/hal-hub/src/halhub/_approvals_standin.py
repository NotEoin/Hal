"""Phase-1 stand-in for 01-security's `halhub.approvals`: requests, answers, grants and the router.

Copies the published shapes of 01-security.md, "The approvals module", with the same names
and fields: `ApprovalRequest`, `Decision(verdict, scope)` (RESOLUTIONS B1), the `Approver`
protocol with `shows_all_args`, `GrantStore` and `ApprovalRouter`. Phase 2 replaces this module
with an import change in the hub. The key functions live in `_policy_standin` beside
`canonical_json` (re-exported here) because the stand-in `decide` needs them too.

Design decisions:

- Grants for `once` and `session` live only in memory in the owning process; nothing pending
  is written to disk (RESOLUTIONS B3, REQ-HUB-21).
- The hub, not the router, adds the grant and writes the `approval_request` and `approval`
  records (04-hub "One call", step 7), because the grant records the tool's security digest
  and `ApprovalRequest` does not carry the tool metadata; this also audits answers from an
  approver that is a bare surface. The router writes `approval_mismatch`, the one record only
  it can see.
- Missing until 01 lands: the control socket (`hal approve`), the `notify` surface, routine
  grant persistence in `routine-grants.jsonl`, revocation and `hal approvals grants`. With no
  control socket a host process's only surfaces are the ones its front end adds.
"""

from __future__ import annotations

import logging
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from halkit.timeutil import utc_now

from ._policy_standin import Grant, Origin, Scope, Taint, Tier, approval_key, security_digest

log = logging.getLogger(__name__)

__all__ = [
    "ApprovalRequest",
    "ApprovalRouter",
    "Approver",
    "Decision",
    "GrantStore",
    "approval_key",
    "escape",
    "new_grant_id",
    "new_request_id",
    "security_digest",
]

ID_HEX_BYTES = 8  # 16 hex characters: `apr_` request ids and `g_` grant ids (01 "Approval binding")
# C0 and C1 controls, DEL, bidirectional controls and zero-width characters (REQ-SEC-25)
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def new_request_id() -> str:
    """`apr_` + 16 hex: typed by people in `hal approve ID`."""
    return "apr_" + secrets.token_hex(ID_HEX_BYTES)


def new_grant_id() -> str:
    """`g_` + 16 hex."""
    return "g_" + secrets.token_hex(ID_HEX_BYTES)


def escape(text: str, *, multiline: bool = True) -> str:
    """`text` with control, bidirectional and zero-width characters shown as `\\uXXXX`.

    Stand-in for `halkit.termsafe.escape` (01 "Display rules"): tabs show as `\\t` and, in
    single-line views, newlines as `\\n`.
    """
    text = text.replace("\t", "\\t")
    if not multiline:
        text = text.replace("\n", "\\n")
    return _UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


@dataclass(frozen=True)
class ApprovalRequest:
    """One host-originated approval request, rendered by the host with every argument."""

    id: str
    session: str
    origin: Origin
    channel: str | None
    pack: str
    pack_version: str
    tool: str
    tier: Tier
    key: str
    reason: str
    args: Mapping[str, object]
    display: str
    summary: str
    content: bool
    challenge: str | None
    scopes: tuple[Scope, ...]
    taint: Taint
    guard: str | None
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class Decision:
    """An approver's answer; `key` must echo the request's key (REQ-SEC-23)."""

    request_id: str
    key: str
    verdict: Literal["allow", "deny"]
    scope: Scope | None
    source: str
    note: str = ""


class Approver(Protocol):
    """An approval surface, or a router over several (01 "Approval surfaces and protocol")."""

    name: str
    shows_all_args: bool

    def available(self) -> bool:
        """True when this surface can show a request now."""
        ...

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Blocks until the human answers or `timeout_s` passes."""
        ...

    def withdraw(self, request_id: str) -> None:
        """Another surface answered or the request expired."""
        ...


class GrantStore:
    """`once` and `session` grants in memory; `consume` makes a `once` grant single use."""

    def __init__(self, routine_file: Path | None = None, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.routine_file = routine_file  # 01 persists routine grants there; the stand-in does not
        self._clock = clock
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def snapshot(self, session: str) -> tuple[Grant, ...]:
        """Live grants usable by `session`: its own `once` and `session` grants and every routine grant."""
        now = self._clock()
        with self._lock:
            return tuple(
                g
                for g in self._grants.values()
                if g.expires > now and (g.scope is Scope.ROUTINE or g.session == session)
            )

    def add(self, grant: Grant) -> None:
        """Stores `grant`; raises `ValueError` for a grant id already present."""
        with self._lock:
            if grant.id in self._grants:
                raise ValueError(f"grant {grant.id} already exists")
            self._grants[grant.id] = grant

    def consume(self, grant_id: str) -> bool:
        """For a `once` grant: True exactly once, while unexpired; True for live other scopes."""
        now = self._clock()
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None or grant.expires <= now:
                return False
            if grant.scope is Scope.ONCE:
                if grant.used:
                    return False
                self._grants[grant_id] = replace(grant, used=True)
            return True

    def end_session(self, session: str) -> None:
        """Drops every `once` and `session` grant of `session`."""
        with self._lock:
            for gid in [gid for gid, g in self._grants.items() if g.session == session]:
                del self._grants[gid]


class ApprovalRouter:
    """Offers a request to every available surface that may show it; the first matching answer wins."""

    name = "router"
    shows_all_args = True

    def __init__(
        self,
        surfaces: Sequence[Approver],
        *,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self.surfaces = tuple(surfaces)
        self._audit = audit

    def available(self) -> bool:
        """True when any surface is available."""
        return any(s.available() for s in self.surfaces)

    def surfaces_for(self, request: ApprovalRequest) -> tuple[Approver, ...]:
        """Available surfaces allowed to show `request`; a `content` request needs `shows_all_args`."""
        return tuple(s for s in self.surfaces if s.available() and (s.shows_all_args or not request.content))

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """The first answer whose id and key match; `deny` from `timeout` or `no-approver` otherwise."""
        surfaces = self.surfaces_for(request)
        if not surfaces:
            return Decision(request.id, request.key, "deny", None, "no-approver")
        answers: queue.Queue[tuple[Approver, Decision | BaseException]] = queue.Queue()

        def ask(surface: Approver) -> None:
            try:
                answers.put((surface, surface.decide(request, timeout_s)))
            except Exception as exc:  # a broken surface counts as no answer, never as allow
                answers.put((surface, exc))

        for surface in surfaces:
            threading.Thread(
                target=ask, args=(surface,), name=f"approval-{surface.name}", daemon=True
            ).start()
        deadline = time.monotonic() + timeout_s
        pending = len(surfaces)
        winner: Decision | None = None
        while pending and winner is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                surface, answer = answers.get(timeout=remaining)
            except queue.Empty:
                break
            pending -= 1
            if isinstance(answer, BaseException):
                log.warning("approval surface %s failed: %s", surface.name, answer)
                continue
            if answer.request_id != request.id or answer.key != request.key:
                self._mismatch(request, answer, surface)
                continue
            winner = replace(answer, source=surface.name)
        for surface in surfaces:
            surface.withdraw(request.id)
        if winner is not None:
            return winner
        source = "timeout" if pending else "no-approver"
        return Decision(request.id, request.key, "deny", None, source)

    def _mismatch(self, request: ApprovalRequest, answer: Decision, surface: Approver) -> None:
        log.warning("approval %s: answer from %s did not match the request", request.id, surface.name)
        if self._audit is not None:
            self._audit(
                "approval_mismatch",
                approval=request.id,
                key_expected=request.key,
                key_got=answer.key,
                source=surface.name,
            )

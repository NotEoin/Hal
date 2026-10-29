"""Approvals: requests, answers, keys, grants, the router and the control socket (01-security, B1).

The one approvals module of the release. 02-sandbox, 04-hub and 06-core-cli use these types
and do not define their own. An approval is always a host-originated `ApprovalRequest`
rendered by the host with every argument; the answer is a `Decision` that echoes the
request's id and key (REQ-SEC-22, REQ-SEC-23). There is no yes-matcher and no "always" scope.

`request_json` and `decision_from_json` are the wire forms ("Approval surfaces and protocol"):
the same object on every transport. A client's `source` is never read; the host-side code of
the surface sets it.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from ..policy.model import Origin, Scope, Taint, Tier
from .grants import (
    GrantError,
    GrantStore,
    RoutineGrantView,
    UnsafeGrantFile,
    new_grant_id,
    once_grant,
    routine_grant,
    session_grant,
)
from .keys import approval_key, security_digest

WIRE_VERSION = 1
ID_HEX_BYTES = 8  # `apr_` + 16 hex (01 "Approval binding")
CHALLENGE_RANGE = 100  # two digits (REQ-SEC-94)
VOICE_MAX_CHARS = 300  # REQ-SEC-94: the longest display voice may speak
NOTIFY_MAX_CHARS = 300  # REQ-SEC-30: the longest display a desktop notification may show


def new_request_id() -> str:
    """`apr_` + 16 hex: typed by people in `hal approve ID`."""
    return "apr_" + secrets.token_hex(ID_HEX_BYTES)


def new_challenge() -> str:
    """A host-generated two-digit voice challenge (REQ-SEC-94); never enters model context."""
    return f"{secrets.randbelow(CHALLENGE_RANGE):02d}"


@dataclass(frozen=True)
class ApprovalRequest:
    """One host-originated approval request, rendered by the host with every argument."""

    id: str  # "apr_" + 16 hex
    session: str  # 12-hex session id
    origin: Origin
    channel: str | None
    pack: str
    pack_version: str
    tool: str
    tier: Tier
    key: str  # approval key (REQ-SEC-19)
    reason: str  # rule and one sentence, from the policy Decision
    args: Mapping[str, object]  # canonical, validated arguments
    display: str  # host-rendered, escaped text of every argument (REQ-SEC-25)
    summary: str  # one host-made sentence, for notifications and logs (voice speaks display)
    content: bool  # True when any parameter has content = true
    challenge: str | None  # voice only; sent only to host surfaces
    scopes: tuple[Scope, ...]  # subset of (ONCE, SESSION)
    taint: Taint
    guard: str | None  # last guard label of the session, for display
    created_at: str  # UTC Z
    expires_at: str  # UTC Z


@dataclass(frozen=True)
class Decision:
    """An approver's answer; `key` must echo the request's key (REQ-SEC-23).

    Not `halhub.policy.Decision`, which is the policy's verdict for a call.
    """

    request_id: str
    key: str
    verdict: Literal["allow", "deny"]
    scope: Scope | None  # ONCE or SESSION when verdict is "allow", else None
    source: str  # set by host code: the Approver's name, "timeout" or "no-approver"
    note: str = ""  # optional text from the user, shown to the model on deny


class Approver(Protocol):
    """An approval surface, or a router over several (01 "Approval surfaces and protocol")."""

    name: str
    shows_all_args: bool  # REQ-SEC-94

    def available(self) -> bool:
        """True when this surface can show a request now."""
        ...

    def decide(self, request: ApprovalRequest, timeout_s: float) -> Decision:
        """Blocks until the human answers or `timeout_s` passes."""
        ...

    def withdraw(self, request_id: str) -> None:
        """Another surface answered or the request expired."""
        ...


def request_json(request: ApprovalRequest) -> dict[str, object]:
    """The request as the `approval_request` wire object."""
    return {
        "type": "approval_request",
        "v": WIRE_VERSION,
        "id": request.id,
        "session": request.session,
        "origin": str(request.origin),
        "channel": request.channel,
        "pack": request.pack,
        "pack_version": request.pack_version,
        "tool": request.tool,
        "tier": str(request.tier),
        "key": request.key,
        "reason": request.reason,
        "args": dict(request.args),
        "display": request.display,
        "summary": request.summary,
        "content": request.content,
        "challenge": request.challenge,
        "scopes": [str(s) for s in request.scopes],
        "taint": request.taint.name.lower(),
        "guard": request.guard,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
    }


def decision_json(decision: Decision) -> dict[str, object]:
    """The answer as the `approval_decision` wire object (without `source`, which hosts set)."""
    return {
        "type": "approval_decision",
        "v": WIRE_VERSION,
        "request_id": decision.request_id,
        "key": decision.key,
        "verdict": decision.verdict,
        "scope": str(decision.scope) if decision.scope else None,
        "note": decision.note,
    }


def decision_from_json(obj: Mapping[str, object], *, source: str) -> Decision:
    """Parses an `approval_decision` object; `source` comes from the host, never the message.

    Raises `ValueError` for a malformed object, an unknown verdict or scope, or an `allow`
    without a scope.
    """
    verdict = obj.get("verdict")
    if verdict not in ("allow", "deny"):
        raise ValueError(f"verdict must be allow or deny, got {verdict!r}")
    request_id, key = obj.get("request_id"), obj.get("key")
    if not isinstance(request_id, str) or not isinstance(key, str):
        raise ValueError("request_id and key must be strings")
    raw_scope = obj.get("scope")
    scope = None
    if verdict == "allow":
        if raw_scope not in (Scope.ONCE.value, Scope.SESSION.value):
            raise ValueError(f"an allow needs scope once or session, got {raw_scope!r}")
        scope = Scope(raw_scope)
    note = obj.get("note", "")
    return Decision(request_id, key, verdict, scope, source, note if isinstance(note, str) else "")


from .router import ApprovalRouter  # noqa: E402 - the router imports the types above

__all__ = [
    "NOTIFY_MAX_CHARS",
    "VOICE_MAX_CHARS",
    "ApprovalRequest",
    "ApprovalRouter",
    "Approver",
    "Decision",
    "GrantError",
    "GrantStore",
    "RoutineGrantView",
    "UnsafeGrantFile",
    "approval_key",
    "decision_from_json",
    "decision_json",
    "new_challenge",
    "new_grant_id",
    "new_request_id",
    "once_grant",
    "request_json",
    "routine_grant",
    "security_digest",
    "session_grant",
]

"""Session tokens: short-lived, scoped credentials for boxed sessions (REQ-GW-60, 61, 62).

Decisions: the registry lives in memory only, so a gateway restart ends every session (the
owner mints a new one; the forwarder reports `token_rejected`). Only the SHA-256 of the secret
is kept, and every comparison uses `hmac.compare_digest`. A session token narrows its caller
on every listener, the unix socket included, so no token widens a principal.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .errors import BadRequest, Forbidden, TooManyRequests, Unauthorized

PREFIX = "hal-sess-"
ID_BYTES = 4  # 8 lowercase hex characters
CLASSES = ("interactive", "service")
MAX_LABEL = 200


class SessionUnknown(Unauthorized):
    """The session token matches no live session."""

    code = "session_unknown"


class SessionExpired(Unauthorized):
    """The session token has expired."""

    code = "session_expired"


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Session:
    """One minted session; `secret_hash` is the only trace of the secret."""

    id: str
    label: str
    secret_hash: str
    aliases: tuple[str, ...]
    allow_remote: bool
    max_request_bytes: int
    max_concurrent: int
    cls: str
    lease: bool
    created: float
    expires: float
    active: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def alias_allowed(self, alias: str) -> bool:
        """True when `alias` matches one of the session's glob patterns."""
        return any(fnmatch.fnmatchcase(alias, pattern) for pattern in self.aliases)

    def public(self) -> dict[str, object]:
        """The session as listed by `GET /hal/v1/sessions` (no token)."""
        return {
            "id": self.id, "label": self.label, "aliases": list(self.aliases),
            "allow_remote": self.allow_remote, "max_request_bytes": self.max_request_bytes,
            "max_concurrent": self.max_concurrent, "class": self.cls, "lease": self.lease,
            "created": _utc(self.created), "expires": _utc(self.expires), "active": self.active,
        }  # fmt: skip


class SessionRegistry:
    """Mints, finds and revokes sessions; tells the GPU manager about leases."""

    def __init__(
        self,
        defaults: Mapping[str, object],
        *,
        clock: Callable[[], float] = time.time,
        on_lease: Callable[[Session], None] | None = None,
        on_lease_end: Callable[[str], None] | None = None,
    ) -> None:
        self.defaults, self.clock = defaults, clock
        self._on_lease, self._on_lease_end = on_lease, on_lease_end
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def mint(self, spec: Mapping[str, object]) -> tuple[Session, str]:
        """Creates a session from the request body `spec`; returns it and the token (shown once)."""
        label = spec.get("label")
        if not isinstance(label, str) or not label or len(label) > MAX_LABEL:
            raise BadRequest("label must be a non-empty string")
        aliases = spec.get("aliases", ["*"])
        if not isinstance(aliases, list) or not aliases or not all(isinstance(a, str) for a in aliases):
            raise BadRequest("aliases must be a non-empty list of strings")
        cls = spec.get("class", "interactive")
        if cls not in CLASSES:
            raise BadRequest(f"class must be one of {CLASSES}")
        numbers = {}
        for key, default in (
            ("max_request_bytes", "max_request_bytes"),
            ("max_concurrent", "max_concurrent"),
            ("ttl_s", "ttl_s"),
        ):
            value = spec.get(key, self.defaults[default])
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BadRequest(f"{key} must be a positive integer")
            numbers[key] = value
        flags = {k: spec.get(k, False) for k in ("allow_remote", "lease")}
        if not all(isinstance(v, bool) for v in flags.values()):
            raise BadRequest("allow_remote and lease must be booleans")
        self.reap()
        secret = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(
            id=secrets.token_hex(ID_BYTES),
            label=label,
            secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
            aliases=tuple(aliases),
            allow_remote=flags["allow_remote"],
            max_request_bytes=numbers["max_request_bytes"],
            max_concurrent=numbers["max_concurrent"],
            cls=str(cls),
            lease=flags["lease"],
            created=now,
            expires=now + numbers["ttl_s"],
        )
        with self._lock:
            self._sessions[session.id] = session
        if session.lease and self._on_lease:
            self._on_lease(session)
        return session, f"{PREFIX}{session.id}.{secret}"

    def find(self, token: str) -> Session:
        """The live session for `token`; raises `SessionUnknown` or `SessionExpired`."""
        ident, _, secret = token.removeprefix(PREFIX).partition(".")
        with self._lock:
            session = self._sessions.get(ident)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        if session is None or not hmac.compare_digest(session.secret_hash, digest):
            raise SessionUnknown("this session token is not known; ask for a new one")
        if self.clock() >= session.expires:
            self.revoke(session.id)
            raise SessionExpired("this session token has expired; ask for a new one")
        return session

    def revoke(self, session_id: str) -> bool:
        """Removes a session; True when it existed."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None and session.lease and self._on_lease_end:
            self._on_lease_end(session.id)
        return session is not None

    def reap(self) -> None:
        """Drops expired sessions (and their leases)."""
        now = self.clock()
        with self._lock:
            dead = [s.id for s in self._sessions.values() if now >= s.expires]
        for ident in dead:
            self.revoke(ident)

    def listing(self) -> list[dict[str, object]]:
        """Live sessions without tokens."""
        self.reap()
        with self._lock:
            return [s.public() for s in self._sessions.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def enter(self, session: Session) -> None:
        """Counts a request; raises `TooManyRequests` beyond `max_concurrent` (REQ-GW-61)."""
        with session.lock:
            if session.active >= session.max_concurrent:
                raise TooManyRequests(f"this session allows {session.max_concurrent} requests at once")
            session.active += 1

    def leave(self, session: Session) -> None:
        """Ends the count of one request."""
        with session.lock:
            session.active -= 1

    @staticmethod
    def check_scope(session: Session, alias: str, remote: bool) -> None:
        """Raises `Forbidden` when the alias or a remote backend is outside the session's scope."""
        if not session.alias_allowed(alias):
            raise Forbidden(f"this session may not use model {alias!r}")
        if remote and not session.allow_remote:
            raise Forbidden("this session may not use remote backends")


def aliases_of(session: Session, names: Sequence[str]) -> list[str]:
    """The names in `names` that the session may use."""
    return [n for n in names if session.alias_allowed(n)]

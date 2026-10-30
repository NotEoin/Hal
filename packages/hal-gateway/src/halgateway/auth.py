"""Who is calling: principals, the owner token file, peer credentials, Host and Origin checks.

Decisions:

- On the unix socket the peer uid proves the owner (REQ-GW-02); a header token is ignored
  there, so a stale owner token in a host pack cannot break it. On TCP only a token proves it.
- Secrets are compared with `hmac.compare_digest` (REQ-GW-06).
- Session and client tokens (`hal-sess-`, `hal-gwc-`) are recognised by prefix but their
  registries come with later plan steps; until then such a token is refused with 401.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import stat
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from halkit import files

from .errors import Forbidden, Unauthorized

log = logging.getLogger(__name__)

OWNER_PREFIX = "hal-gw-"
SESSION_PREFIX = "hal-sess-"
CLIENT_PREFIX = "hal-gwc-"
TOKEN_BYTES = 32  # secrets.token_urlsafe argument: 256 bits
TOKEN_FILE_MODE = 0o600
PEERCRED_FORMAT = "3i"  # struct ucred: pid, uid, gid
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

Listener = Literal["unix", "tcp"]
PeerUid = Callable[[socket.socket], int]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of one request."""

    kind: Literal["owner", "client", "session", "anonymous"]
    listener: Listener


def peer_uid(sock: socket.socket) -> int:
    """Returns the uid of the process on the other end of a unix socket (`SO_PEERCRED`)."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(PEERCRED_FORMAT))
    return struct.unpack(PEERCRED_FORMAT, raw)[1]


def token_file_is_private(path: Path) -> bool:
    """True when `path` is a regular file with no group or other permission bits."""
    mode = path.stat().st_mode
    return stat.S_ISREG(mode) and not mode & 0o077


def load_or_create_owner_token(path: Path) -> str:
    """Returns the owner token in `path`, writing a fresh 0600 file at the first start."""
    path = path.expanduser()
    if not path.exists():
        token = OWNER_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        files.write_atomic(path, token + "\n", mode=TOKEN_FILE_MODE)
        log.info("created the owner token file %s", path)
        return token
    return path.read_text(encoding="ascii").strip()


def bearer_token(headers: Mapping[str, str]) -> str:
    """Returns the token from `Authorization: Bearer` or `X-Api-Key`, or an empty string."""
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("X-Api-Key", "").strip()


def principal_for(
    listener: Listener,
    peer: int | None,
    headers: Mapping[str, str],
    owner_token: str | None,
    *,
    own_uid: int | None = None,
) -> Principal:
    """Returns the caller's principal; raises `Unauthorized` for a token that does not match.

    A request with no token on TCP is `anonymous` (it may reach `/health` only).
    """
    own = os.getuid() if own_uid is None else own_uid
    token = bearer_token(headers)
    if listener == "unix":
        if peer == own:
            return Principal("owner", listener)
        return Principal("anonymous", listener)
    if not token:
        return Principal("anonymous", listener)
    if token.startswith((SESSION_PREFIX, CLIENT_PREFIX)):
        raise Unauthorized("this token is not known to the gateway")
    if owner_token and hmac.compare_digest(token.encode(), owner_token.encode()):
        return Principal("owner", listener)
    raise Unauthorized("invalid API key")


def check_host_and_origin(
    headers: Mapping[str, str], port: int, allow_hosts: list[str], allow_origins: list[str]
) -> None:
    """Raises `Forbidden` for a Host or Origin a loopback client would not send (REQ-GW-08).

    Blocks DNS rebinding (a foreign Host) and browsers (any Origin not listed).
    """
    allowed = {f"{name}:{port}" for name in LOOPBACK_NAMES} | set(allow_hosts)
    host = headers.get("Host", "")
    if host not in allowed:
        raise Forbidden(f"Host {host!r} is not allowed; see [gateway].allow_hosts")
    origin = headers.get("Origin")
    if origin is not None and origin not in allow_origins:
        raise Forbidden(f"Origin {origin!r} is not allowed; see [gateway].allow_origins")

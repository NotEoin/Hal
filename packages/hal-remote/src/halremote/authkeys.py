"""Render, parse, merge and remove the `authorized_keys` lines hal-remote owns.

Every line Hal writes carries the comment `hal-remote:<client>:<role>`; that tag is the only
thing `merge` and `remove` act on, so foreign lines survive byte for byte (REQ-RMT-07). The
option order is fixed and golden-tested (REQ-RMT-04, REQ-RMT-05). Why each option is there:
docs/design/07-remote.md, "authorized_keys lines".
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from halkit import files

from .keys import PublicKey, parse_public_key
from .model import ROLES, Forward, Role, check_client_name

try:
    import fcntl
except ImportError:  # Windows: the desktop side of hal-remote is Linux-only, the import must still work
    fcntl = None  # type: ignore[assignment]  # not a module on this platform

log = logging.getLogger(__name__)

LOCK_TIMEOUT_S = 10.0  # REQ-RMT-07
LOCK_POLL_S = 0.05
SSH_DIR_MODE = 0o700
FILE_MODE = 0o600
TAG_PREFIX = "hal-remote:"
PERMITLISTEN = "127.0.0.1:1"  # a port a normal user cannot bind, so no -R listener can open
FORBIDDEN_PATH_CHARS = ('"', "\\", "\n", "\r", "\0")
_TAGGED = re.compile(r"(?:^|\s)ssh-ed25519 [A-Za-z0-9+/=]+ hal-remote:([a-z0-9-]+):(tunnel|mcp)\s*$")
_FROM_OK = re.compile(r"^[0-9a-fA-F.:*?/,!-]+$")  # CIDRs and sshd patterns; no quote or space


class AuthKeysError(ValueError):
    """A value cannot be written into an `authorized_keys` option safely."""


class LockTimeout(TimeoutError):
    """`authorized_keys` stayed locked for `LOCK_TIMEOUT_S`."""


@dataclass(frozen=True)
class HalKeyLine:
    """A parsed Hal-owned line."""

    client: str
    role: Role
    options: tuple[str, ...]
    key: PublicKey


def _checked_path(path: Path) -> str:
    """Returns `path` as text after REQ-RMT-09 checks: absolute, no quote, backslash or newline."""
    text = str(path)
    if not Path(text).is_absolute() and not text.startswith("/"):
        raise AuthKeysError(f"path {text!r} must be absolute")
    if any(c in text for c in FORBIDDEN_PATH_CHARS):
        raise AuthKeysError(f"path {text!r} contains a character that cannot be quoted safely")
    return text


def render_line(
    role: Role,
    *,
    client: str,
    key: PublicKey,
    from_pattern: str | None,
    forwards: Sequence[Forward],
    serve_path: Path,
    hub_path: Path,
    expires: date | None = None,
) -> str:
    """Renders the `authorized_keys` line for one role of `client`."""
    if role not in ROLES:
        raise AuthKeysError(f"unknown role {role!r}")
    check_client_name(client)
    options = ["restrict"]
    if role == "tunnel":
        options.append("port-forwarding")
    if from_pattern is not None:
        if not _FROM_OK.fullmatch(from_pattern):
            raise AuthKeysError(f"invalid from= pattern {from_pattern!r}")
        options.append(f'from="{from_pattern}"')
    if expires is not None:
        options.append(f'expiry-time="{expires:%Y%m%d}Z"')
    if role == "tunnel":
        if not forwards:
            raise AuthKeysError("the tunnel key needs at least one forward")
        options += [f'permitopen="127.0.0.1:{f.remote_port}"' for f in forwards]
        options += [f'permitlisten="{PERMITLISTEN}"', 'command="/bin/false"']
    else:
        serve = shlex.quote(_checked_path(serve_path))
        hub = shlex.quote(_checked_path(hub_path))
        options.append(f'command="{serve} serve --client {client} --hub {hub}"')
    return f"{','.join(options)} {key.type} {key.blob} {TAG_PREFIX}{client}:{role}"


def _split_options(text: str) -> tuple[str, ...]:
    """Splits an option string on commas outside double quotes."""
    parts, buf, quoted, i = [], [], False, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and quoted and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        if c == "," and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return tuple(p for p in parts if p)


def _parse_line(line: str) -> HalKeyLine | None:
    match = _TAGGED.search(line)
    if match is None:
        return None
    key = parse_public_key(line[match.start() :].strip())
    options = _split_options(line[: match.start()].strip())
    return HalKeyLine(match.group(1), match.group(2), options, key)  # type: ignore[arg-type]  # regex limits to Role


def parse(text: str) -> tuple[list[HalKeyLine], int]:
    """Returns the Hal-owned lines and the number of other non-blank lines."""
    owned: list[HalKeyLine] = []
    other = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parsed = _parse_line(raw)
        if parsed is None:
            other += 1
        else:
            owned.append(parsed)
    return owned, other


def _is_tagged(line: str, client: str) -> bool:
    match = _TAGGED.search(line.rstrip("\r\n"))
    return match is not None and match.group(1) == client


def remove(existing: str, client: str) -> tuple[str, int]:
    """Drops the lines tagged for `client`; returns the new text and how many were dropped."""
    kept, dropped = [], 0
    for line in existing.splitlines(keepends=True):
        if _is_tagged(line, client):
            dropped += 1
        else:
            kept.append(line)
    return "".join(kept), dropped


def merge(existing: str, client: str, lines: Sequence[str]) -> str:
    """Replaces the lines of `client` with `lines`; every other byte is kept."""
    check_client_name(client)
    for line in lines:
        if "\n" in line or "\r" in line or not _is_tagged(line, client):
            raise AuthKeysError(f"line is not a single hal-remote line for client {client!r}")
    base, _ = remove(existing, client)
    if base and not base.endswith("\n"):
        base += "\n"
    return base + "".join(line + "\n" for line in lines)


@contextmanager
def _lock(path: Path, timeout_s: float) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{path} is still locked after {timeout_s} s") from None
                time.sleep(LOCK_POLL_S)
        yield
    finally:
        os.close(fd)


def write(path: Path, text: str, *, lock_timeout_s: float = LOCK_TIMEOUT_S) -> None:
    """Writes `text` to `path` atomically under a lock; leaves the file 0600 and its directory 0700."""
    path = Path(path)
    path.parent.mkdir(mode=SSH_DIR_MODE, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, SSH_DIR_MODE)
    with _lock(path, lock_timeout_s):
        files.write_atomic(path, text.encode("utf-8"), mode=FILE_MODE)
    log.info("wrote %s", path)


def update(path: Path, client: str, lines: Sequence[str], *, lock_timeout_s: float = LOCK_TIMEOUT_S) -> None:
    """Read-modify-write of `path` under one lock: replaces `client`'s lines with `lines`."""
    path = Path(path)
    path.parent.mkdir(mode=SSH_DIR_MODE, parents=True, exist_ok=True)
    with _lock(path, lock_timeout_s):
        # bytes, not text mode: universal newlines would rewrite CRLF lines that are not ours
        current = path.read_bytes().decode("utf-8") if path.exists() else ""
        files.write_atomic(path, merge(current, client, lines).encode("utf-8"), mode=FILE_MODE)
    if os.name != "nt":
        os.chmod(path.parent, SSH_DIR_MODE)

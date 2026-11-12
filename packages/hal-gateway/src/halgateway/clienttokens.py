"""Named client tokens in `<state>/gateway/tokens.json` (REQ-GW-66, 67).

Decisions: only the SHA-256 of the secret is stored; the CLI edits the file itself under
`fcntl.flock` with an atomic replace, so it works while the gateway is down, and the gateway
`stat`s the file on every check and re-reads it when the mtime or inode changed, so a revoke
takes effect on the next request without a restart.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from halkit import files

PREFIX = "hal-gwc-"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CLASSES = ("interactive", "service", "batch")
LOCK_TIMEOUT_S = 5.0
LOCK_POLL_S = 0.05
FILE_MODE = 0o600
SECRET_BYTES = 32


class TokenError(ValueError):
    """A client token operation was refused."""


@dataclass(frozen=True)
class ClientToken:
    """One issued client token (never its secret)."""

    name: str
    created: str  # UTC Z
    allow_remote: bool
    cls: str

    def public(self) -> dict[str, object]:
        """The listing form: names and dates, never secrets."""
        return {
            "name": self.name,
            "created": self.created,
            "allow_remote": self.allow_remote,
            "class": self.cls,
        }


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ClientTokens:
    """Named client tokens; re-read when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._seen: tuple[int, int] | None = None
        self._rows: dict[str, dict[str, object]] = {}

    # --- reading -------------------------------------------------------------------------

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            doc = json.loads(self.path.read_text(encoding="ascii"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise TokenError(f"{self.path} is unreadable: {exc}") from exc
        rows = doc.get("tokens") if isinstance(doc, dict) else None
        if not isinstance(rows, dict):
            raise TokenError(f"{self.path} has no `tokens` object")
        return rows

    def _fresh(self) -> dict[str, dict[str, object]]:
        try:
            st = self.path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_ino)
        except FileNotFoundError:
            stamp = None
        if stamp != self._seen:
            self._rows = self._read() if stamp else {}
            self._seen = stamp
        return self._rows

    def list(self) -> list[ClientToken]:
        """Every issued token, oldest first."""
        rows = self._fresh()
        return sorted((self._token(n, r) for n, r in rows.items()), key=lambda t: t.created)

    @staticmethod
    def _token(name: str, row: dict[str, object]) -> ClientToken:
        return ClientToken(name, str(row["created"]), bool(row["allow_remote"]), str(row["class"]))

    def check(self, token: str) -> ClientToken | None:
        """The client for `token`, or None when it is unknown or revoked."""
        name, _, secret = token.removeprefix(PREFIX).partition(".")
        try:
            row = self._fresh().get(name)
        except TokenError:
            return None  # a damaged file fails closed
        if row is None or not hmac.compare_digest(str(row["hash"]), _hash(secret)):
            return None
        return self._token(name, row)

    # --- writing (CLI) -------------------------------------------------------------------

    def _locked(self):  # type: ignore[no-untyped-def]
        """Context manager: an exclusive `flock` on a sibling lock file, 5 s timeout."""
        return _Lock(self.path.with_name(self.path.name + ".lock"))

    def _write(self, rows: dict[str, dict[str, object]]) -> None:
        files.write_atomic(self.path, json.dumps({"tokens": rows}, indent=1) + "\n", mode=FILE_MODE)

    def issue(
        self, name: str, *, allow_remote: bool = False, cls: str = "interactive", rotate: bool = False
    ) -> str:
        """Creates a token and returns it once; an existing name is an error unless `rotate`."""
        if not NAME_RE.fullmatch(name):
            raise TokenError(f"name {name!r} must match {NAME_RE.pattern}")
        if cls not in CLASSES:
            raise TokenError(f"class must be one of {CLASSES}")
        secret = secrets.token_urlsafe(SECRET_BYTES)
        with self._locked():
            rows = self._read()
            if name in rows and not rotate:
                raise TokenError(f"a token named {name!r} exists; use --rotate to replace it")
            created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows[name] = {
                "hash": _hash(secret),
                "created": created,
                "allow_remote": allow_remote,
                "class": cls,
            }
            self._write(rows)
        return f"{PREFIX}{name}.{secret}"

    def revoke(self, name: str) -> bool:
        """Removes a token; True when it existed."""
        with self._locked():
            rows = self._read()
            if name not in rows:
                return False
            del rows[name]
            self._write(rows)
        return True


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path, self.fd = path, -1

    def __enter__(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise TokenError(f"could not lock {self.path} within {LOCK_TIMEOUT_S:.0f} s") from None
                time.sleep(LOCK_POLL_S)

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)

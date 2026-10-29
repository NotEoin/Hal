"""Reading and creating secret files (API tokens, passwords) without leaking them.

A secret file must be private to its owner. `read_secret` refuses one that is group- or
world-readable or owned by someone else, instead of using a value others may have seen
(`~/.config/hal/config.toml` was found at mode 0664 holding a bearer secret, which this check
would have caught). The file is checked on the open descriptor, so a swap between check and
read cannot get around it.
"""

from __future__ import annotations

import contextlib
import os
import secrets as stdlib_secrets
import tempfile
from pathlib import Path

DEFAULT_TOKEN_BYTES = 32  # 256 bits, the usual size for a bearer token
PRIVATE_FILE_MODE = 0o600


class UnsafeSecretError(Exception):
    """A secret file is readable by others or owned by another user."""


def read_secret(path: Path) -> str:
    """Returns the content of `path` minus one trailing newline.

    Raises `UnsafeSecretError` when the file has group or other permission bits or another
    owner, `FileNotFoundError` when it is missing, and `ValueError` when it is empty.
    """
    path = Path(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if os.name != "nt":
            if info.st_uid != os.getuid():
                raise UnsafeSecretError(f"{path} is owned by uid {info.st_uid}, not by you")
            if info.st_mode & 0o077:
                mode = info.st_mode & 0o777
                raise UnsafeSecretError(f"{path} has mode {mode:04o}; run: chmod 600 {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
    finally:
        os.close(fd)
    text = raw.decode("utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise ValueError(f"{path} is empty")
    return text


def generate_token(path: Path, *, nbytes: int = DEFAULT_TOKEN_BYTES) -> str:
    """Returns the token in `path`, creating the file (mode 0600) with a random one if missing.

    Creation is race-free: two processes that both find the file missing end up with the same
    token, because the loser of the exclusive link reads the winner's file.
    """
    path = Path(path)
    if nbytes < 16:
        raise ValueError("a token needs at least 16 bytes of randomness")
    with contextlib.suppress(FileNotFoundError):
        return read_secret(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = stdlib_secrets.token_urlsafe(nbytes)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
            handle.write(token + "\n")
        try:
            os.link(tmp_name, path)  # fails if another process created it first
        except FileExistsError:
            return read_secret(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
    return token

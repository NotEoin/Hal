"""SSH public key parsing and ed25519 key generation with the system `ssh-keygen`.

Only `ssh-ed25519` keys are accepted (REQ-RMT-01): the add-on creates its own keys, so a wider
parser would only widen what an `authorized_keys` line can smuggle in. The fingerprint is
computed in Python from the key blob, so listing and checking keys needs no subprocess.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

from halkit import proc

log = logging.getLogger(__name__)

KEY_TYPE = "ssh-ed25519"
KEYGEN_TIMEOUT_S = 30.0  # design 07, Python interfaces: ssh-keygen gets 30 s
PRIVATE_MODE = 0o600
MAX_COMMENT_LEN = 200  # a comment is copied into authorized_keys; keep a hostile one short

Runner = Callable[..., CompletedProcess[bytes]]  # `halkit.proc.run`-compatible


class KeyFormatError(ValueError):
    """A line is not a single well-formed `ssh-ed25519` public key."""


@dataclass(frozen=True)
class PublicKey:
    """An `ssh-ed25519` public key: type, base64 blob and comment."""

    type: str
    blob: str
    comment: str

    def fingerprint(self) -> str:
        """The `SHA256:...` fingerprint, as `ssh-keygen -lf` prints it."""
        digest = hashlib.sha256(base64.b64decode(self.blob)).digest()
        return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")

    def line(self) -> str:
        """The key as one `authorized_keys` style line."""
        return f"{self.type} {self.blob} {self.comment}".rstrip()


def _blob_type(blob: bytes) -> str:
    """Reads the type string at the start of an SSH wire-format blob."""
    if len(blob) < 4:
        raise KeyFormatError("key blob is too short")
    (length,) = struct.unpack(">I", blob[:4])
    if length > len(blob) - 4:
        raise KeyFormatError("key blob is truncated")
    return blob[4 : 4 + length].decode("ascii", errors="replace")


def parse_public_key(line: str) -> PublicKey:
    """Parses one public key line; raises `KeyFormatError` for anything but ssh-ed25519."""
    if "\n" in line.strip() or "\r" in line.strip() or "\0" in line:
        raise KeyFormatError("a public key must be a single line")
    parts = line.strip().split(None, 2)
    if len(parts) < 2:
        raise KeyFormatError("expected '<type> <base64> [comment]'")
    key_type, blob, comment = parts[0], parts[1], (parts[2] if len(parts) > 2 else "")
    if key_type != KEY_TYPE:
        raise KeyFormatError(f"unsupported key type {key_type!r}; only {KEY_TYPE} is accepted")
    try:
        raw = base64.b64decode(blob, validate=True)
    except binascii.Error:
        raise KeyFormatError("key blob is not valid base64") from None
    if _blob_type(raw) != KEY_TYPE:
        raise KeyFormatError("key blob does not match its declared type")
    if len(comment) > MAX_COMMENT_LEN or not comment.isprintable():
        raise KeyFormatError("key comment is too long or has control characters")
    return PublicKey(key_type, blob, comment)


def keygen_argv(path: Path, comment: str, ssh_keygen: Path) -> list[str]:
    """The `ssh-keygen` argv that makes a passphrase-less ed25519 pair at `path`."""
    return [str(ssh_keygen), "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)]


def generate(
    path: Path,
    *,
    comment: str,
    ssh_keygen: Path,
    timeout_s: float = KEYGEN_TIMEOUT_S,
    runner: Runner = proc.run,
) -> PublicKey:
    """Creates a key pair at `path` and `path.pub`; refuses to replace either file.

    Never reads `~/.ssh`: the caller names the path under the profile directory.
    """
    path = Path(path)
    pub = path.with_name(path.name + ".pub")
    for existing in (path, pub):
        if existing.exists():
            raise FileExistsError(f"{existing} exists; refusing to replace a key (use --rotate)")
    if not comment.isprintable() or len(comment) > MAX_COMMENT_LEN:
        raise KeyFormatError("key comment is too long or has control characters")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    runner(keygen_argv(path, comment, ssh_keygen), timeout=timeout_s, check=True)
    if os.name != "nt":
        os.chmod(path, PRIVATE_MODE)
    log.info("generated ed25519 key %s", pub)
    return parse_public_key(pub.read_text(encoding="ascii"))

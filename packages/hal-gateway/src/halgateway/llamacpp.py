"""Verify a downloaded llama.cpp archive against the repo's lock file (REQ-SEC-78).

Standard library only, so `setup.sh` can run it with the system `python3` before any virtual
environment exists. An archive with no entry in the lock, or with a different sha256, is
refused: the installer must stop before extracting it.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from pathlib import Path

LOCK_PATH = Path(__file__).with_name("llamacpp.lock.toml")
HASH_CHUNK = 1 << 20  # archives are ~750 MB; hash in 1 MiB chunks
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LockError(ValueError):
    """The lock file is invalid, or an archive is unlisted or does not match it."""


def load_lock(path: Path = LOCK_PATH) -> tuple[str, dict[str, str]]:
    """Return the pinned tag and the sha256 of each asset file name."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LockError(f"cannot read {path}: {exc}") from exc
    tag, assets = data.get("tag"), data.get("assets", {})
    if not isinstance(tag, str) or not re.fullmatch(r"b\d+", tag):
        raise LockError(f"{path}: tag must look like b11512")
    for name, digest in assets.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise LockError(f"{path}: asset {name} needs a 64 hex sha256")
    return tag, dict(assets)


def verify_archive(archive: Path, tag: str, lock: Path = LOCK_PATH) -> None:
    """Raise `LockError` unless `archive` is a locked asset of `tag` with the locked sha256."""
    pinned, assets = load_lock(lock)
    if tag != pinned:
        raise LockError(f"tag {tag} is not the pinned release {pinned}")
    expected = assets.get(archive.name)
    if expected is None:
        raise LockError(f"{archive.name} has no sha256 in the lock file")
    h = hashlib.sha256()
    with archive.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise LockError(f"{archive.name}: sha256 mismatch, refusing to extract")


def main(argv: list[str]) -> int:
    """`llamacpp.py verify TAG FILE`: exit 0 when the archive matches, 1 with a message if not."""
    if len(argv) != 4 or argv[1] != "verify":
        print("usage: llamacpp.py verify TAG FILE", file=sys.stderr)  # noqa: T201 - CLI entry point
        return 2
    try:
        verify_archive(Path(argv[3]), argv[2])
    except (LockError, OSError) as exc:
        print(f"llama.cpp lock check failed: {exc}", file=sys.stderr)  # noqa: T201 - CLI entry point
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

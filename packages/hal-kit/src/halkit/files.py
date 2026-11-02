"""Crash-safe file writes and a rotating, lock-guarded JSONL appender.

`write_atomic` never leaves a half-written file: readers see the old content or the new,
even after a crash. `append_jsonl` is for logs that several processes write (the audit log,
turn logs): one `flock`-guarded `write` per record, rotation by size, ASCII-only output so a
log survives any terminal or tool. `locked` takes the same lock for a whole-file rewrite.

`append_jsonl` needs `fcntl`, which Windows lacks; it raises there instead of writing an
unguarded log. Nothing that runs on a Windows laptop appends to a shared log.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]  # the module is POSIX-only; checked at use

LOCK_TIMEOUT_S = 5.0  # a writer holds the lock for one write, so 5 s means a stuck process
LOCK_POLL_S = 0.01
DEFAULT_MAX_BYTES = 16 * 2**20  # bounds disk use for logs that have no rotation today
DEFAULT_KEEP = 5


class LockTimeout(TimeoutError):
    """The log lock was not free within `LOCK_TIMEOUT_S`."""


def write_atomic(path: Path, data: bytes | str, *, mode: int = 0o600) -> None:
    """Writes `data` to `path` through a temp file, fsync and rename; the file ends with `mode`.

    The temp file is in the same directory so the rename stays on one filesystem. The parent
    directory is created (0700) when missing.
    """
    path = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return  # Windows cannot open a directory for fsync
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError("append_jsonl needs fcntl, which this platform does not have")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{lock_path} is still locked after {LOCK_TIMEOUT_S} s") from None
                time.sleep(LOCK_POLL_S)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Holds the lock `append_jsonl` takes for `path`, for a read-modify-write of the whole file.

    Raises `LockTimeout` after 5 s, like `append_jsonl`. Used to compact an append-only file
    without losing a record another process appends meanwhile.
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(path.with_name(path.name + ".lock")):
        yield


def _rotate(path: Path, keep: int) -> None:
    oldest = path.with_name(f"{path.name}.{keep}")
    with contextlib.suppress(FileNotFoundError):
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
    if keep > 0:
        os.replace(path, path.with_name(f"{path.name}.1"))
    else:
        path.unlink()


def append_jsonl(
    path: Path,
    record: Mapping[str, object],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> None:
    """Appends `record` as one ASCII JSON line to `path`, rotating at `max_bytes`.

    Holds an exclusive lock on `<path>.lock` for the write; raises `LockTimeout` if another
    process keeps it for more than 5 s. Rotation renames `path` to `path.1`, `path.1` to
    `path.2`, and so on, and drops files beyond `keep`.
    """
    path = Path(path)
    line = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(path.with_name(path.name + ".lock")):
        try:
            if path.stat().st_size >= max_bytes:
                _rotate(path, keep)
        except FileNotFoundError:
            # no file yet, so nothing to rotate
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

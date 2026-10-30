"""HTTPS download of one pinned file into a temporary file next to its target (REQ-GW-79).

Decisions:

- Only `https` URLs are opened, including every redirect hop (Hugging Face redirects to a
  CDN); a hop to another scheme is refused. No credentials are sent.
- The bytes go to a temporary file in the target directory, are hashed on the way, and are
  renamed into place only when the size and sha256 match; otherwise the temporary file is
  deleted and `DownloadError` is raised. An existing file is never replaced here.
- The opener is injectable so tests serve bytes without a network.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10.0  # design: connect timeout of a pull
READ_TIMEOUT_S = 60.0  # design: timeout per read of a pull
MAX_REDIRECTS = 5
CHUNK_BYTES = 1 << 20
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

Opener = Callable[[str], tuple[BinaryIO, int | None]]
Progress = Callable[[str, int, int], None]


class DownloadError(RuntimeError):
    """The download failed, or its bytes do not match the manifest."""


def open_https(url: str) -> tuple[BinaryIO, int | None]:
    """Opens `url` (https only, redirects followed) and returns the body and its length."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise DownloadError(f"refusing non-https URL {url!r}")
        conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=CONNECT_TIMEOUT_S)
        try:
            conn.connect()
            conn.sock.settimeout(READ_TIMEOUT_S)  # type: ignore[union-attr]  # connect() set it
            conn.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""))
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise DownloadError(f"cannot fetch {parts.hostname}: {exc}") from exc
        if resp.status in REDIRECT_STATUSES and resp.getheader("Location"):
            url = urljoin(url, resp.getheader("Location"))  # type: ignore[arg-type]  # checked above
            conn.close()
            continue
        if resp.status != 200:
            conn.close()
            raise DownloadError(f"{parts.hostname} answered {resp.status}")
        length = resp.getheader("Content-Length")
        return resp, int(length) if length and length.isdigit() else None  # type: ignore[return-value]
    raise DownloadError(f"more than {MAX_REDIRECTS} redirects")


def fetch(
    url: str,
    target: Path,
    *,
    sha256: str,
    size: int,
    name: str,
    progress: Progress | None = None,
    opener: Opener = open_https,
) -> None:
    """Downloads `url` to `target` if size and sha256 match; never replaces an existing file."""
    if not url.startswith("https://"):
        raise DownloadError(f"refusing non-https URL {url!r}")
    if target.exists():
        raise DownloadError(f"{target} exists; will not overwrite it")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    done = 0
    try:
        body, _length = opener(url)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := body.read(CHUNK_BYTES):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(name, done, size)
        finally:
            body.close()
        if done != size or digest.hexdigest() != sha256:
            raise DownloadError(f"{name}: downloaded bytes do not match the manifest (size or sha256)")
        os.link(tmp, target)  # fails if `target` appeared meanwhile, unlike a rename
        log.info("downloaded %s", name)
    except OSError as exc:
        raise DownloadError(f"{name}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

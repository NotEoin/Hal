"""Install the pinned llama.cpp release, checking each archive against the lock file (REQ-SEC-78).

`install` downloads the archives of one variant (`cuda`, `rocm`, `vulkan` or `cpu`) of the tag
in `llamacpp.lock.toml` from GitHub, refuses any archive with no entry in the lock or a different
sha256 before it is extracted, and puts the result in `<root>/llama-<tag>/`, the folder the
router's `server` default names. Decisions:

- Standard library only, so it runs before any optional dependency is installed.
- Extraction uses the tarfile `data` filter: no absolute paths, no `..`, no device files, no
  links that leave the folder. A leading `llama-<tag>/` folder in an archive is stripped, so the
  binary and the CUDA runtime archive land side by side.
- The folder appears in one rename after every archive is in, so a failed install leaves none.
- `variant_for` picks the variant from the GPUs Hal detects: NVIDIA gets CUDA, AMD gets ROCm,
  no GPU gets the CPU build; Vulkan is for the user to ask for.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

LOCK_PATH = Path(__file__).with_name("llamacpp.lock.toml")
HASH_CHUNK = 1 << 20  # archives are ~750 MB; hash in 1 MiB chunks
RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{name}"
DOWNLOAD_TIMEOUT_S = 60.0  # per read; a stalled download fails instead of hanging
VARIANTS = ("cuda", "rocm", "vulkan", "cpu")
Opener = Callable[..., object]
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


def variant_for(vendors: Sequence[str]) -> str:
    """The build for GPUs of these vendors (`nvidia`, `amd`): CUDA, ROCm, else the CPU build."""
    if "nvidia" in vendors:
        return "cuda"
    return "rocm" if "amd" in vendors else "cpu"


def files_of(variant: str, lock: Path = LOCK_PATH) -> tuple[str, list[str]]:
    """`(tag, archive names)` of `variant`; raises `LockError` for an unknown variant."""
    tag, _ = load_lock(lock)
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    names = (data.get("variants") or {}).get(variant)
    if not isinstance(names, list) or not names:
        raise LockError(f"no llama.cpp {variant} build is pinned; choose one of {', '.join(VARIANTS)}")
    return tag, [str(n) for n in names]


def _download(url: str, target: Path, opener: Opener) -> None:
    with opener(url, timeout=DOWNLOAD_TIMEOUT_S) as resp, target.open("wb") as out:  # type: ignore[attr-defined]
        shutil.copyfileobj(resp, out, HASH_CHUNK)


def _extract(archive: Path, into: Path, tag: str) -> None:
    """Extracts `archive` into `into`, dropping a leading `llama-<tag>/` folder."""
    with tarfile.open(archive) as tar:
        staging = into.parent / f".{archive.name}.x"
        tar.extractall(staging, filter="data")
    inner = staging / f"llama-{tag}"
    source = inner if inner.is_dir() else staging
    for item in source.iterdir():
        os.replace(item, into / item.name)
    shutil.rmtree(staging)


def install(
    root: Path, variant: str, *, opener: Opener = urllib.request.urlopen, lock: Path = LOCK_PATH
) -> Path:
    """Installs the pinned `variant` into `<root>/llama-<tag>/` and returns that folder.

    Does nothing when `llama-server` is already there. Raises `LockError` for an archive that
    does not match the lock and `OSError` for a failed download or extraction; nothing is left
    behind on failure.
    """
    tag, names = files_of(variant, lock)
    target = Path(root) / f"llama-{tag}"
    if (target / "llama-server").is_file():
        return target
    Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".llama-install-") as tmp:
        work = Path(tmp)
        folder = work / f"llama-{tag}"
        folder.mkdir()
        for name in names:
            archive = work / name
            _download(RELEASE_URL.format(tag=tag, name=name), archive, opener)
            verify_archive(archive, tag, lock)
            _extract(archive, folder, tag)
        if not (folder / "llama-server").is_file():
            raise LockError(f"the {variant} archives of {tag} hold no llama-server")
        os.replace(folder, target)
    return target


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

"""The model manifest `models.toml` and its sha256 verification (REQ-SEC-79, REQ-GW-75, REQ-GW-77).

The gateway only reads this file; it never writes user config. Design decisions:

- A weight file loads only when it has an entry here and its sha256 matches. The hash is
  recomputed whenever size, mtime or inode differ from the last verified state, so a file swapped
  after the first load is caught. A cheap `stat` is the only cost on the hot path.
- The verified state lives in a separate cache file under the state directory, never in the
  user's manifest.
- Pickle formats and extensions are refused by name before any hash is computed (REQ-SEC-80).
- Ollama models are pinned by their manifest digest and compared with the digest `/api/tags`
  reports; a moved tag is refused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = 1
FORMATS = frozenset({"gguf", "safetensors"})
PICKLE_SUFFIXES = frozenset({".pt", ".pth", ".ckpt", ".bin", ".pkl", ".pickle", ".joblib", ".npy"})
HASH_CHUNK = 1 << 20  # read weights 1 MiB at a time so a 20 GB file never sits in memory
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEYS = {"name", "file", "format", "sha256", "size", "source", "hf_file", "template_sha256", "profile"}
_FILE_KEYS = {"file", "format", "sha256", "size"}


class ManifestError(ValueError):
    """The manifest is invalid, or a model is unlisted or fails verification."""


@dataclass(frozen=True)
class ModelEntry:
    """One `[[model]]` table."""

    name: str
    source: str
    file: Path | None
    format: str | None
    sha256: str | None
    size: int | None
    profile: str | None = None
    template_sha256: str | None = None
    hf_file: str | None = None

    @property
    def ollama_digest(self) -> str | None:
        """The pinned `sha256:<hex>` digest of an `ollama:` entry, else None."""
        if not self.source.startswith("ollama:"):
            return None
        return self.source.rsplit("@", 1)[1]


def _entry(raw: dict, models_dir: Path) -> ModelEntry:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("a [[model]] table has no name")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ManifestError(f"model {name}: unknown keys {sorted(unknown)}")
    source = raw.get("source", "local")
    if not isinstance(source, str):
        raise ManifestError(f"model {name}: source must be a string")
    if source.startswith("ollama:"):
        if _FILE_KEYS & set(raw):
            raise ManifestError(f"model {name}: file, format, sha256 and size are not allowed for ollama")
        if not re.search(r"@sha256:[0-9a-f]{64}$", source):
            raise ManifestError(f"model {name}: an ollama source must end in @sha256:<64 hex>")
        return ModelEntry(name, source, None, None, None, None, raw.get("profile"))
    missing = _FILE_KEYS - set(raw)
    if missing:
        raise ManifestError(f"model {name}: missing {sorted(missing)}")
    fmt, digest, size = raw["format"], raw["sha256"], raw["size"]
    path = Path(raw["file"])
    if path.suffix.lower() in PICKLE_SUFFIXES or fmt not in FORMATS:
        raise ManifestError(f"model {name}: format {fmt!r} / file {path.name} is refused (no pickle)")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ManifestError(f"model {name}: sha256 must be 64 lowercase hex characters")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ManifestError(f"model {name}: size must be a non-negative integer")
    if not path.is_absolute():
        path = models_dir / path
    return ModelEntry(
        name,
        source,
        path,
        fmt,
        digest,
        size,
        raw.get("profile"),
        raw.get("template_sha256") or None,
        raw.get("hf_file"),
    )


def load_manifest(path: Path, models_dir: Path) -> dict[str, ModelEntry]:
    """Parse `models.toml` into entries by name; raises `ManifestError` on any problem."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if data.get("schema") != SCHEMA:
        raise ManifestError(f"{path}: schema must be {SCHEMA}")
    extra = set(data) - {"schema", "model"}
    if extra:
        raise ManifestError(f"{path}: unknown keys {sorted(extra)}")
    entries: dict[str, ModelEntry] = {}
    for raw in data.get("model", []):
        entry = _entry(raw, models_dir)
        if entry.name in entries:
            raise ManifestError(f"duplicate model name {entry.name}")
        entries[entry.name] = entry
    return entries


def sha256_file(path: Path) -> str:
    """The hex sha256 of a file, read in chunks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class Verifier:
    """Verifies manifest entries and remembers what it verified in `verified.json`."""

    def __init__(self, entries: dict[str, ModelEntry], cache_path: Path) -> None:
        """Bind the entries and the cache file (read lazily, written atomically)."""
        self.entries = entries
        self.cache_path = cache_path
        self.hashes = 0  # how many full hashes were computed, for tests and `status`

    def _load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _store(self, cache: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.cache_path)

    def verify_path(self, path: Path, *, full: bool = False) -> ModelEntry:
        """Return the entry for a weight file once its hash matches; raise `ManifestError` if not."""
        resolved = Path(os.path.realpath(path))
        entry = next(
            (e for e in self.entries.values() if e.file and Path(os.path.realpath(e.file)) == resolved),
            None,
        )
        if entry is None or entry.sha256 is None:
            raise ManifestError(f"{path} has no entry in models.toml")
        try:
            st = resolved.stat()
        except OSError as exc:
            raise ManifestError(f"{entry.name}: file is missing: {exc}") from exc
        if st.st_size != entry.size:
            raise ManifestError(f"{entry.name}: size {st.st_size} differs from the manifest")
        state = [st.st_size, st.st_mtime_ns, st.st_ino]
        cache = self._load()
        if not full and cache.get(entry.name) == {"sha256": entry.sha256, "state": state}:
            return entry
        self.hashes += 1
        if sha256_file(resolved) != entry.sha256:
            cache.pop(entry.name, None)
            self._store(cache)
            raise ManifestError(f"{entry.name}: sha256 mismatch, refusing to load")
        cache[entry.name] = {"sha256": entry.sha256, "state": state}
        self._store(cache)
        log.info("verified %s", entry.name)
        return entry

    def check_ollama(self, name: str, reported_digest: str) -> ModelEntry:
        """Require an `ollama:` entry for `name` whose pinned digest equals the reported one."""
        entry = self.entries.get(name)
        pinned = entry.ollama_digest if entry else None
        if pinned is None:
            raise ManifestError(f"ollama model {name} is not pinned in models.toml")
        if pinned != reported_digest:
            raise ManifestError(f"ollama model {name}: digest moved from the pinned one")
        return entry

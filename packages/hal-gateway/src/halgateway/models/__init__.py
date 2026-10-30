"""Model manifest operations: list, plan, pull, verify, pin (design 05, "Model manifest").

Decisions:

- This is the only code that writes `models.toml`, atomically (temp file, then replace) with
  mode 0600. The gateway's `serve` path only reads it.
- Pinning (`add_local`, `record_ollama`) states trust in a file or digest, so callers (the
  `hal models` CLI) show what is pinned and ask first; these functions do not ask.
- The functions take a `Store` (manifest path, models directory, state directory) instead of
  reading globals, so tests and a second install use their own.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import download
from .manifest import (
    FORMATS,
    PICKLE_SUFFIXES,
    ManifestError,
    ModelEntry,
    Verifier,
    load_manifest,
    sha256_file,
)

log = logging.getLogger(__name__)

HF_SOURCE = re.compile(r"^hf:([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)@([0-9a-f]{40})$")
HF_URL = "https://huggingface.co/{repo}/resolve/{commit}/{path}"
MANIFEST_MODE = 0o600
OLLAMA_PULL_TIMEOUT_S = 3600.0  # a first pull of a large model is slow; Ollama streams until done
_TOML_KEYS = ("name", "file", "format", "sha256", "size", "source", "hf_file", "template_sha256", "profile")


@dataclass(frozen=True)
class Store:
    """Where the manifest, the weights and the verification cache live."""

    manifest: Path
    models_dir: Path
    verified: Path


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of verifying one entry."""

    name: str
    ok: bool
    detail: str


def list_entries(store: Store) -> list[ModelEntry]:
    """All manifest entries in file order; empty when there is no manifest yet."""
    if not store.manifest.exists():
        return []
    return list(load_manifest(store.manifest, store.models_dir).values())


def _select(store: Store, names: Sequence[str]) -> list[ModelEntry]:
    entries = {e.name: e for e in list_entries(store)}
    unknown = [n for n in names if n not in entries]
    if unknown:
        raise ManifestError(f"not in the manifest: {', '.join(unknown)}")
    return [entries[n] for n in names] if names else list(entries.values())


def _quote(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write(store: Store, entries: Sequence[ModelEntry]) -> None:
    lines = ["schema = 1", ""]
    for e in entries:
        fields = {
            "name": e.name,
            "file": _relative(e.file, store.models_dir) if e.file else None,
            "format": e.format,
            "sha256": e.sha256,
            "size": e.size,
            "source": e.source,
            "hf_file": e.hf_file,
            "template_sha256": e.template_sha256,
            "profile": e.profile,
        }
        lines.append("[[model]]")
        lines += [f"{k} = {_quote(fields[k])}" for k in _TOML_KEYS if fields[k] not in (None, "")]
        lines.append("")
    store.manifest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=store.manifest.parent, prefix=".models.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines))
    os.chmod(tmp, MANIFEST_MODE)
    os.replace(tmp, store.manifest)


def _relative(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def plan(store: Store, names: Sequence[str]) -> list[ModelEntry]:
    """The entries `pull` would fetch: `hf:` files that are missing, and `ollama:` models."""
    return [
        e
        for e in _select(store, names)
        if e.source.startswith("ollama:")
        or (e.file is not None and not e.file.exists() and e.source != "local")
    ]


def pull(
    store: Store,
    names: Sequence[str],
    *,
    progress: download.Progress | None = None,
    opener: download.Opener = download.open_https,
    ollama=None,  # type: ignore[no-untyped-def]  # an `OllamaBackend`; needed for `ollama:` entries
) -> None:
    """Downloads the named `hf:` entries and pulls `ollama:` ones, verifying each (REQ-GW-79)."""
    for entry in _select(store, names):
        if entry.source.startswith("ollama:"):
            _pull_ollama(entry, ollama)
            continue
        if entry.file is None or entry.sha256 is None or entry.size is None:
            raise ManifestError(f"{entry.name}: nothing to pull")
        match = HF_SOURCE.match(entry.source)
        if match is None:
            raise ManifestError(
                f"{entry.name}: source {entry.source!r} is not pinned; use hf:<repo>@<40-hex commit>"
            )
        if entry.file.exists():
            if sha256_file(entry.file) == entry.sha256:
                continue
            raise download.DownloadError(f"{entry.file} exists with another hash; will not overwrite it")
        url = HF_URL.format(repo=match.group(1), commit=match.group(2), path=entry.hf_file or entry.file.name)
        download.fetch(
            url,
            entry.file,
            sha256=entry.sha256,
            size=entry.size,
            name=entry.name,
            progress=progress,
            opener=opener,
        )


def _pull_ollama(entry: ModelEntry, ollama) -> None:  # type: ignore[no-untyped-def]
    from .. import relay

    if ollama is None:
        raise ManifestError(f"{entry.name}: an Ollama backend is needed to pull it")
    model = entry.source[len("ollama:") :].rsplit("@", 1)[0]
    relay.call_json(
        ollama, "POST", "/api/pull", {"model": model, "stream": False}, timeout_s=OLLAMA_PULL_TIMEOUT_S
    )
    reported = {t.get("name"): str(t.get("digest", "")) for t in ollama.tags()}.get(model, "")
    reported = reported if reported.startswith("sha256:") else f"sha256:{reported}"
    Verifier({entry.name: entry}, Path(os.devnull)).check_ollama(entry.name, reported)


def verify(store: Store, names: Sequence[str], *, full: bool) -> list[VerifyResult]:
    """Verifies the named file entries; `full` re-hashes, else only changed files are (REQ-GW-80)."""
    entries = _select(store, names)
    verifier = Verifier({e.name: e for e in entries}, store.verified)
    results = []
    for e in entries:
        if e.file is None:
            results.append(VerifyResult(e.name, True, "ollama: digest is checked on each request"))
            continue
        try:
            verifier.verify_path(e.file, full=full)
            results.append(VerifyResult(e.name, True, "ok"))
        except ManifestError as exc:
            results.append(VerifyResult(e.name, False, str(exc)))
    return results


def add_local(store: Store, path: Path, *, name: str | None = None, profile: str | None = None) -> ModelEntry:
    """Hashes an existing weight file and records it with `source = "local"`."""
    path = Path(path)
    fmt = path.suffix.lower().lstrip(".")
    if path.suffix.lower() in PICKLE_SUFFIXES or fmt not in FORMATS:
        raise ManifestError(f"{path.name}: only .gguf and .safetensors files can be pinned")
    if not path.is_file():
        raise ManifestError(f"{path} is not a file")
    entries = list_entries(store)
    entry_name = name or path.stem
    if any(e.name == entry_name for e in entries):
        raise ManifestError(f"a model named {entry_name} is already in the manifest")
    entry = ModelEntry(
        entry_name, "local", path.resolve(), fmt, sha256_file(path), path.stat().st_size, profile
    )
    _write(store, [*entries, entry])
    return entry


def record_ollama(store: Store, names: Sequence[str], ollama) -> list[ModelEntry]:  # type: ignore[no-untyped-def]
    """Pins the digests Ollama reports now for `names`; an already pinned name is refreshed."""
    digests = {str(t.get("name")): str(t.get("digest", "")) for t in ollama.tags()}
    entries = {e.name: e for e in list_entries(store)}
    added = []
    for model in names:
        digest = digests.get(model) or digests.get(f"{model}:latest")
        if not digest:
            raise ManifestError(f"Ollama does not have {model}; pull it first")
        digest = digest.removeprefix("sha256:")
        old = entries.get(model)
        entry = (
            replace(old, source=f"ollama:{model}@sha256:{digest}")
            if old
            else ModelEntry(model, f"ollama:{model}@sha256:{digest}", None, None, None, None)
        )
        entries[model] = entry
        added.append(entry)
    _write(store, list(entries.values()))
    return added

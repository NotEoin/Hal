#!/usr/bin/env python3
"""
hal_rag.py — Hal's local retrieval core (shared library, not a CLI).

Everything the RAG layer needs, in one dependency-light module:

  * EMBEDDINGS  — talk to the local Ollama embedding model (`/api/embed`, batched, with the
    nomic "search_document:" / "search_query:" task prefixes). A deterministic `--mock` mode
    lets the whole pipeline be tested offline (no model, no network).
  * CHUNKING    — turn a note into retrieval chunks: strip the YAML frontmatter, keep the
    useful metadata (title, course, code, section, role), and split the body on headings /
    size with a little overlap so passages stay coherent.
  * VECTOR STORE — a tiny numpy-backed store: an L2-normalised float32 matrix (so cosine
    similarity is a single dot product) plus a `chunks.jsonl` sidecar of metadata and a
    `manifest.json` of per-file content hashes for cheap incremental re-indexing.

No server, no native extensions: the index is three plain files under `System/.rag/`. Brute-
force search over a personal vault (a few thousand chunks) is exact and sub-millisecond, so we
skip approximate-ANN complexity entirely.

Imported by `vault-index.py`, `ask.py` and `hal-mcp.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

# --- Config ------------------------------------------------------------------
OLLAMA_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("HAL_EMBED_MODEL", "nomic-embed-text")
INDEX_DIRNAME = os.path.join("System", ".rag")     # lives inside the (git-ignored) vault

# Folders we never index (caches, staging, the index itself, raw imports).
SKIP_DIRS = {".rag", ".obsidian", ".trash", "_Import-Staging",
             ".pdf-text", ".docx-text", ".pptx-text", ".html-text"}

MOCK_DIM = 256   # dimensionality of deterministic mock vectors


# --- Embeddings --------------------------------------------------------------
def _mock_vector(text: str, dim: int = MOCK_DIM) -> list[float]:
    """A stable pseudo-embedding derived from the text, so offline tests are reproducible
    and semantically-ish meaningful (token hashing into a bag-of-words vector)."""
    vec = np.zeros(dim, dtype=np.float32)
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    n = np.linalg.norm(vec)
    if n:
        vec /= n
    return vec.tolist()


def _prefix(text: str, kind: str, model: str) -> str:
    """nomic-embed-text wants task prefixes; harmless to skip for other models."""
    if "nomic" in model.lower():
        return f"{'search_query' if kind == 'query' else 'search_document'}: {text}"
    return text


def embed_texts(texts: list[str], *, kind: str = "document",
                model: str | None = None, mock: bool = False,
                timeout: int = 300, batch: int = 64) -> list[list[float]]:
    """Embed a list of strings. `kind` is 'document' or 'query'."""
    model = model or EMBED_MODEL
    if mock:
        return [_mock_vector(t) for t in texts]

    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = [_prefix(t, kind, model) for t in texts[i:i + batch]]
        out.extend(_embed_call(chunk, model, timeout))
    return out


def _embed_call(inputs: list[str], model: str, timeout: int) -> list[list[float]]:
    """One call to Ollama's batch embed endpoint, with a graceful fallback to the
    older single-prompt endpoint if the batch route isn't available."""
    body = json.dumps({"model": model, "input": inputs}).encode("utf-8")
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        embs = data.get("embeddings")
        if embs:
            return embs
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    # Fallback: older /api/embeddings, one prompt at a time.
    res = []
    for text in inputs:
        b = json.dumps({"model": model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(f"{OLLAMA_URL}/api/embeddings", data=b,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res.append(json.load(r).get("embedding", []))
    return res


# --- Chunking ----------------------------------------------------------------
@dataclass
class Chunk:
    id: str          # stable: <relpath>#<ordinal>
    path: str        # vault-relative path of the source note
    title: str
    course: str
    code: str
    section: str
    role: str
    heading: str     # nearest markdown heading above this chunk
    text: str


# Notes we don't bother embedding: empty concept stubs and pure-Dataview shell pages.
# They're navigation scaffolding, not content — indexing them just adds noise.
SKIP_STATUS = {"stub"}
SKIP_TYPE = {"topic", "dashboard", "index"}


def should_index(text: str) -> bool:
    fm, _ = _split_frontmatter(text)
    if fm.get("status", "").strip().lower() in SKIP_STATUS:
        return False
    if fm.get("type", "").strip().lower() in SKIP_TYPE:
        return False
    return True


def _split_frontmatter(text: str) -> tuple[dict, str]:
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
                if m:
                    fm[m.group(1)] = m.group(2).strip()
            body = text[end + 4:]
    return fm, body


def _clean(body: str) -> str:
    # Drop our generated breadcrumb line and dataview code fences — they aren't worth embedding.
    body = re.sub(r"```dataview.*?```", "", body, flags=re.S)
    lines = [ln for ln in body.splitlines()
             if not ln.strip().startswith("*[[")          # breadcrumb
             and not ln.strip().startswith("<!--")]       # html comments
    return "\n".join(lines)


def chunk_note(relpath: str, text: str, *, max_chars: int = 1200,
               overlap: int = 150) -> list[Chunk]:
    """Split one note into overlapping, heading-aware chunks."""
    fm, body = _split_frontmatter(text)
    body = _clean(body)

    title = fm.get("title", "")
    if not title:
        m = re.search(r"^#\s+(.*)$", body, re.M)
        title = m.group(1).strip() if m else Path(relpath).stem

    meta = dict(path=relpath, title=title,
                course=fm.get("course", ""), code=fm.get("code", ""),
                section=fm.get("section", ""), role=fm.get("role", fm.get("type", "")))

    # Walk the body, tracking the nearest heading, accumulating into size-bounded blocks.
    chunks: list[Chunk] = []
    heading = title
    buf: list[str] = []
    buf_len = 0
    ordinal = 0

    def flush():
        nonlocal buf, buf_len, ordinal
        passage = "\n".join(buf).strip()
        if len(passage) >= 40:   # skip near-empty fragments
            chunks.append(Chunk(id=f"{relpath}#{ordinal}", heading=heading,
                                text=passage, **meta))
            ordinal += 1
        # carry a small overlap tail into the next buffer
        tail = passage[-overlap:] if overlap and passage else ""
        buf = [tail] if tail else []
        buf_len = len(tail)

    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        h = re.match(r"^#{1,6}\s+(.*)$", para)
        if h:
            if buf_len:
                flush()
            heading = h.group(1).strip()
            continue
        if buf_len + len(para) + 1 > max_chars and buf_len:
            flush()
        buf.append(para)
        buf_len += len(para) + 1
    if buf_len:
        flush()
    return chunks


# --- Vector store ------------------------------------------------------------
def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class VectorStore:
    """Three files: vectors.npy (N x D float32, L2-normalised), chunks.jsonl (metadata,
    same order), manifest.json ({relpath: filehash} for incremental indexing)."""

    def __init__(self, index_dir: Path):
        self.dir = Path(index_dir)
        self.vec_path = self.dir / "vectors.npy"
        self.meta_path = self.dir / "chunks.jsonl"
        self.manifest_path = self.dir / "manifest.json"
        self.vectors = np.zeros((0, 0), dtype=np.float32)
        self.chunks: list[dict] = []
        self.manifest: dict[str, str] = {}

    # -- persistence --
    def load(self) -> "VectorStore":
        if self.vec_path.exists() and self.meta_path.exists():
            self.vectors = np.load(self.vec_path)
            self.chunks = [json.loads(l) for l in
                           self.meta_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return self

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        np.save(self.vec_path, self.vectors.astype(np.float32))
        with self.meta_path.open("w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        self.manifest_path.write_text(json.dumps(self.manifest, indent=0), encoding="utf-8")

    # -- mutation (incremental) --
    def drop_paths(self, paths: set[str]) -> None:
        if not self.chunks:
            return
        keep = [i for i, c in enumerate(self.chunks) if c["path"] not in paths]
        self.chunks = [self.chunks[i] for i in keep]
        self.vectors = self.vectors[keep] if self.vectors.size else self.vectors
        for p in paths:
            self.manifest.pop(p, None)

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        new = _normalize(np.asarray(vectors, dtype=np.float32))
        self.vectors = new if self.vectors.size == 0 else np.vstack([self.vectors, new])
        self.chunks.extend(asdict(c) for c in chunks)

    # -- query --
    def search(self, query_vec: list[float], k: int = 8) -> list[dict]:
        if self.vectors.size == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        n = np.linalg.norm(q)
        if n:
            q = q / n
        sims = self.vectors @ q
        k = min(k, len(sims))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        out = []
        for i in idx:
            c = dict(self.chunks[int(i)])
            c["score"] = float(sims[int(i)])
            out.append(c)
        return out


# --- Vault walking + hashing -------------------------------------------------
def file_hash(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def iter_vault_notes(vault: Path):
    """Yield (relpath, abspath) for every .md note worth indexing."""
    vault = Path(vault)
    for dp, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                ap = Path(dp) / f
                yield str(ap.relative_to(vault)), ap


def index_dir_for(vault: Path) -> Path:
    return Path(vault) / INDEX_DIRNAME

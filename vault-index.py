#!/usr/bin/env python3
"""
vault-index.py — build / refresh Hal's local semantic index.

Walks your vault, splits each note into retrieval chunks, embeds them with the LOCAL embedding
model (Ollama), and writes a tiny on-disk index under `<vault>/System/.rag/`. That index is
what `hal ask` and the Hal MCP server search.

It's INCREMENTAL: each note's content hash is remembered, so a re-run only re-embeds notes that
changed (and forgets notes you deleted). The first run on a big vault does real work; every run
after is near-instant. Fully local — nothing leaves the machine.

Usage:
  hal index                 # refresh the index (only changed notes)
  hal index --rebuild       # throw the index away and build from scratch
  hal index --mock          # deterministic offline embeddings (wiring test, no model)
  hal index --vault PATH    # index a specific vault
  hal index --embed-model M # use a different Ollama embedding model
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import hal_rag as R

BLUE = "\033[1;36m"; GREEN = "\033[1;32m"; YELLOW = "\033[1;33m"
RED = "\033[1;31m"; DIM = "\033[2m"; OFF = "\033[0m"
def hal(m):  print(f"{BLUE}{m}{OFF}", flush=True)
def ok(m):   print(f"{GREEN}  ✓ {m}{OFF}", flush=True)
def tick(m): print(f"  {m}", flush=True)
def warn(m): print(f"{YELLOW}  ! {m}{OFF}", flush=True)
def err(m):  print(f"{RED}  ✗ {m}{OFF}", flush=True)


def resolve_vault(arg):
    for cand in (arg, os.environ.get("HAL_VAULT", ""),
                 str(Path(__file__).resolve().parent / "Vault")):
        if cand:
            p = Path(cand).expanduser()
            if p.exists():
                return p
    return None


def main():
    ap = argparse.ArgumentParser(description="Build/refresh Hal's local semantic index.")
    ap.add_argument("--vault", default=None)
    ap.add_argument("--embed-model", default=R.EMBED_MODEL)
    ap.add_argument("--rebuild", action="store_true", help="ignore the existing index")
    ap.add_argument("--mock", action="store_true", help="deterministic offline embeddings")
    ap.add_argument("--max-chars", type=int, default=1200)
    args = ap.parse_args()

    vault = resolve_vault(args.vault)
    if not vault:
        err("I don't know where your vault is. Set HAL_VAULT or pass --vault.")
        sys.exit(1)

    index_dir = R.index_dir_for(vault)
    store = R.VectorStore(index_dir)
    if not args.rebuild:
        store.load()

    hal(f"Indexing your brain at {vault} …")
    if args.mock:
        warn("mock embeddings (offline wiring test) — search will be lexical-ish, not semantic")

    # 1) take stock of the vault as it is now. We skip notes that aren't worth
    #    embedding (empty concept stubs, Dataview dashboards, index shells) so the
    #    index holds real content, not navigation scaffolding. A skipped note that
    #    was previously indexed is treated like a deletion (dropped below).
    current = {}                      # relpath -> (abspath, hash)
    skipped = 0
    for rel, ap in R.iter_vault_notes(vault):
        try:
            raw = ap.read_bytes()
        except OSError:
            continue
        if not R.should_index(raw.decode("utf-8", errors="ignore")):
            skipped += 1
            continue
        current[rel] = (ap, hashlib.md5(raw).hexdigest())

    have = store.manifest               # relpath -> hash from last run
    changed = [r for r, (_, h) in current.items() if have.get(r) != h]
    deleted = [r for r in have if r not in current]

    # 2) forget deleted + changed notes (changed ones get re-added below)
    store.drop_paths(set(deleted) | set(changed))

    tick(f"{DIM}{len(current)} indexable notes · {len(changed)} new/changed · "
         f"{len(deleted)} removed · {skipped} skipped (stubs/dashboards){OFF}")
    if not changed and not deleted:
        ok("Index already up to date — nothing to do.")
        store.save()
        return

    # 3) chunk the changed notes
    all_chunks = []
    for rel in changed:
        ap, _h = current[rel]
        try:
            text = ap.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        all_chunks.extend(R.chunk_note(rel, text, max_chars=args.max_chars))

    if all_chunks:
        tick(f"{DIM}embedding {len(all_chunks)} chunks…{OFF}")
        t0 = time.time()
        texts = [c.text if not c.heading else f"{c.title} — {c.heading}\n{c.text}"
                 for c in all_chunks]
        try:
            vecs = R.embed_texts(texts, kind="document",
                                 model=args.embed_model, mock=args.mock)
        except Exception as e:  # noqa: BLE001
            err(f"embedding failed ({e}).")
            if not args.mock:
                err("Is the embedding model pulled?  ollama pull "
                    f"{args.embed_model}   (or run with --mock to test wiring)")
            sys.exit(1)
        store.add(all_chunks, vecs)
        ok(f"Embedded {len(all_chunks)} chunks in {time.time()-t0:.1f}s")

    # 4) record the new hashes + persist
    for rel, (_, h) in current.items():
        store.manifest[rel] = h
    store.save()

    ok(f"Index saved → {index_dir.relative_to(vault)}  "
       f"({len(store.chunks)} chunks from {len(current)} notes)")
    tick(f"{DIM}ask it anything with:  hal ask \"your question\"{OFF}")


if __name__ == "__main__":
    main()

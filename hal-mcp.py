#!/usr/bin/env python3
"""
hal-mcp.py — the Hal MCP server: your local "second brain", exposed as tools.

This turns the same local RAG core that powers `hal ask` into a Model Context Protocol
server, so *any* MCP client (Claude Desktop, Claude Code, another agent) can search and
question your Obsidian vault. Everything stays on the machine: retrieval is the local numpy
index under `<vault>/System/.rag/`, and `ask_vault` answers with the local Ollama model.

Three tools:
  • search_vault(query, k)    — semantic search; returns the top passages with their note
                                title, section and an Obsidian [[wikilink]] to open them.
  • ask_vault(question, k)    — a grounded answer in Hal's voice, citing the notes it used
                                as [[wikilinks]] (refuses to invent when the notes fall short).
  • get_note(path)            — return the full Markdown of one note by its vault-relative path.

Transport is stdio (the MCP default), so it's launched by the client, not run by hand. The
vault is taken from $HAL_VAULT (or the bundled ./Vault). Registered in `Vault/.mcp.json` as:

  "hal": { "command": "uv", "args": ["run", "--with", "mcp",
           "/abs/path/to/hal-mcp.py"], "env": { "HAL_VAULT": "/abs/path/to/Vault" } }

Run it directly only to smoke-test:  HAL_VAULT=./Vault uv run --with mcp ./hal-mcp.py
"""

from __future__ import annotations

import os
from pathlib import Path

import hal_rag as R
import ask as A

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - helpful message when run without the SDK
    raise SystemExit(
        "The 'mcp' package isn't available. Launch this server with:\n"
        "  uv run --with mcp ./hal-mcp.py\n"
        "(that's exactly how Vault/.mcp.json starts it)."
    )


# --- vault + index helpers ---------------------------------------------------
def _resolve_vault() -> Path:
    """Find the vault the same way the CLIs do: $HAL_VAULT, else the bundled ./Vault."""
    for cand in (os.environ.get("HAL_VAULT", ""),
                 str(Path(__file__).resolve().parent / "Vault")):
        if cand:
            p = Path(cand).expanduser()
            if p.exists():
                return p
    raise SystemExit("hal-mcp: no vault found. Set HAL_VAULT to your vault path.")


VAULT = _resolve_vault()
EMBED_MODEL = os.environ.get("HAL_EMBED_MODEL", R.EMBED_MODEL)
CHAT_MODEL = os.environ.get("HAL_MODEL", A.CHAT_MODEL)

mcp = FastMCP("hal")


def _index_is_empty() -> bool:
    store = R.VectorStore(R.index_dir_for(VAULT)).load()
    return store.vectors.size == 0


# --- tools -------------------------------------------------------------------
@mcp.tool()
def search_vault(query: str, k: int = 8) -> str:
    """Semantically search the user's personal Obsidian notes and return the most relevant
    passages. Use this to find what the user has written about a topic before answering from
    your own knowledge. Returns each passage's note title, section, an Obsidian [[wikilink]],
    a similarity score, and the passage text. `k` is how many passages to return (default 8).
    """
    _store, hits = A.retrieve(VAULT, query, k, embed_model=EMBED_MODEL)
    if not hits:
        if _index_is_empty():
            return ("No index yet. Build it on the machine with `hal index` "
                    "(or `python3 vault-index.py`), then try again.")
        return f"No passages found for: {query!r}"

    out = [f"Found {len(hits)} passage(s) for {query!r}:\n"]
    for i, h in enumerate(hits, 1):
        loc = h.get("section") or h.get("course") or ""
        where = f"  ·  {loc}" if loc else ""
        out.append(
            f"### {i}. {h['title']}{where}  (score {h['score']:.3f})\n"
            f"Open: [[{h['title']}]]   ·   path: `{h['path']}`\n\n"
            f"{h['text'].strip()}\n"
        )
    return "\n".join(out)


@mcp.tool()
def ask_vault(question: str, k: int = 8) -> str:
    """Answer a question using ONLY the user's own notes, in Hal's voice, citing each note it
    leaned on as an Obsidian [[wikilink]]. This runs the full local RAG pipeline (retrieve →
    ground → answer with the local model) and will say so plainly if the notes don't cover the
    question rather than inventing an answer. Prefer this for 'what do my notes say about…' or
    'explain X from my course' style questions. `k` is how many passages to retrieve (default 8).
    """
    reply, _used = A.answer(VAULT, question, k=k,
                            embed_model=EMBED_MODEL, model=CHAT_MODEL)
    return reply


@mcp.tool()
def get_note(path: str) -> str:
    """Return the full Markdown content of a single note, addressed by its vault-relative path
    (e.g. 'Areas/Study/2025-26/Databases - CS2040/Week 9 - Normalisation.md'). Paths come
    from `search_vault` results. Use this to read a whole note after search surfaces it.
    """
    rel = path.strip().lstrip("/")
    if not rel.endswith(".md"):
        rel += ".md"
    target = (VAULT / rel).resolve()
    # Stay inside the vault — never read arbitrary files off the machine.
    try:
        target.relative_to(VAULT.resolve())
    except ValueError:
        return f"Refused: '{path}' is outside the vault."
    if not target.exists():
        return f"Not found: '{rel}'. Use search_vault to find the correct path."
    try:
        return target.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return f"Could not read '{rel}': {e}"


if __name__ == "__main__":
    mcp.run()

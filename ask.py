#!/usr/bin/env python3
"""
ask.py — chat with your brain. Ask a question; get an answer grounded in your own notes.

It embeds your question, finds the most relevant passages in the local index (built by
`hal index`), and asks a model to answer USING ONLY those passages — citing each note
it leaned on as an Obsidian [[wikilink]] so you can click straight through. If the notes don't
cover it, it says so rather than inventing an answer.

Retrieval is **always local** — your notes are embedded and searched on your own machine in
every setup profile. Only the final write-up differs: the `24gb` / `12gb` profiles hand the
retrieved passages to your local Ollama model, while the `cloud` profile hands them to
Claude Code (`--answerer claude`), which does mean those passages leave the machine.

Usage:
  hal ask "what is the Diffie-Hellman key exchange?"
  hal ask --k 12 "compare quicksort and mergesort"
  hal ask --show-context "..."   # also print the passages it retrieved
  hal ask --no-llm "..."         # just show what was retrieved (no model call)
  hal ask --mock "..."           # offline wiring test (mock embeddings + plain output)
  hal ask --answerer claude "…"  # force the Claude Code answerer (default in cloud profile)

The retrieval half is reused by the Hal MCP server (`hal-mcp.py`).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import hal_chat
import hal_rag as R

BLUE = "\033[1;36m"; GREEN = "\033[1;32m"; YELLOW = "\033[1;33m"
RED = "\033[1;31m"; DIM = "\033[2m"; OFF = "\033[0m"
def hal(m):  print(f"{BLUE}{m}{OFF}", flush=True)
def warn(m): print(f"{YELLOW}  ! {m}{OFF}", flush=True)
def err(m):  print(f"{RED}  ✗ {m}{OFF}", flush=True)

CHAT_MODEL = os.environ.get("HAL_MODEL", "")

# Which engine writes the final answer. Retrieval is always local either way.
#   ollama  — POST to the local model's /api/chat (the 24gb / 12gb profiles)
#   claude  — shell out to `claude -p` (the cloud profile: no local LLM to call, and this
#             reuses your normal Claude Code login rather than needing a separate API key)
ANSWERER = os.environ.get("HAL_ANSWERER", "ollama")
CLAUDE_CMD = os.environ.get("HAL_CLAUDE_COMMAND", "claude")

ANSWER_SYSTEM = (
    "You are Hal, the user's private study assistant. Answer the question USING ONLY the "
    "context passages provided — they are excerpts from the user's own notes. Be concise and "
    "clear. Cite the notes you use inline as Obsidian [[wikilinks]] using each passage's Title "
    "(shown as 'Source: <Title>'). If the passages don't contain the answer, say so plainly and "
    "suggest what to look for — do NOT use outside knowledge or invent facts. Write clean "
    "Markdown."
)


# --- retrieval (shared shape with the MCP server) ----------------------------
def retrieve(vault: Path, question: str, k: int, *, mock=False, embed_model=None):
    store = R.VectorStore(R.index_dir_for(vault)).load()
    if store.vectors.size == 0:
        return store, []
    qvec = R.embed_texts([question], kind="query", model=embed_model, mock=mock)[0]
    hits = store.search(qvec, k=k)
    return store, hits


def dedupe_by_note(hits, cap):
    """Keep the best passages but don't let one note dominate; cap total passages."""
    seen, out = {}, []
    for h in hits:
        seen[h["path"]] = seen.get(h["path"], 0) + 1
        if seen[h["path"]] <= 2:        # at most 2 passages per note
            out.append(h)
        if len(out) >= cap:
            break
    return out


def build_context(hits) -> str:
    blocks = []
    for h in hits:
        loc = h.get("section") or h.get("course") or ""
        head = f"Source: {h['title']}" + (f"  ({loc})" if loc else "")
        blocks.append(f"{head}\n{h['text']}")
    return "\n\n---\n\n".join(blocks)


def call_claude(system: str, prompt: str, timeout=600) -> str:
    """Answer via headless Claude Code. Used by the `cloud` profile.

    We pass the retrieved passages in the prompt and let Claude Code's own auth apply, so
    there is no second API key to manage. The environment is inherited untouched on purpose:
    if you point Claude Code at a proxy or a gateway, `hal ask` follows it.
    """
    cmd = [CLAUDE_CMD, "-p", "--append-system-prompt", system, prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(
            f"'{CLAUDE_CMD}' is not on your PATH — the cloud profile answers with Claude Code. "
            "Install it, or switch HAL_ANSWERER to 'ollama'.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout}s") from None
    if r.returncode != 0:
        # Claude Code reports things like "Not logged in · Please run /login" on stdout,
        # not stderr, so show both or the failure looks empty and unexplained.
        detail = ((r.stderr or "").strip() + " " + (r.stdout or "").strip()).strip()
        raise RuntimeError(f"claude failed (exit {r.returncode}): {detail[:300] or 'no output'}")
    return (r.stdout or "").strip()


def call_model(messages, model, timeout=600, retries=1):
    """One isolated chat completion against the local model server."""
    return hal_chat.chat(messages, model, temperature=0.2, timeout=timeout,
                         retries=retries)


def answer(vault: Path, question: str, *, k=8, ctx_cap=6, mock=False,
           no_llm=False, embed_model=None, model=None, answerer=None):
    """Returns (answer_text, hits_used). Used by the CLI and the MCP server."""
    _store, hits = retrieve(vault, question, k, mock=mock, embed_model=embed_model)
    if not hits:
        return ("I have no index yet — run `hal index` first so I can read your notes.", [])
    used = dedupe_by_note(hits, ctx_cap)
    context = build_context(used)
    sources = []
    for h in used:
        if h["title"] not in sources:
            sources.append(h["title"])

    if no_llm or mock:
        body = "\n".join(f"- [[{s}]]" for s in sources)
        return (f"_(retrieval only — no model call)_\n\n**Top passages for:** {question}\n\n"
                f"{context}\n\n**Sources**\n{body}", used)

    prompt = f"Context passages from the user's notes:\n\n{context}\n\nQuestion: {question}"
    if (answerer or ANSWERER) == "claude":
        reply = call_claude(ANSWER_SYSTEM, prompt)
    else:
        if not (model or CHAT_MODEL):
            raise RuntimeError(
                "No local model configured. Either set HAL_MODEL / pass --model, or use the "
                "cloud answerer with --answerer claude.")
        reply = call_model([{"role": "system", "content": ANSWER_SYSTEM},
                            {"role": "user", "content": prompt}], model or CHAT_MODEL)
    if "[[" not in reply and sources:    # ensure citations are always present
        reply += "\n\n**Sources:** " + ", ".join(f"[[{s}]]" for s in sources)
    return (reply, used)


def resolve_vault(arg):
    for cand in (arg, os.environ.get("HAL_VAULT", ""),
                 str(Path(__file__).resolve().parent / "Vault")):
        if cand:
            p = Path(cand).expanduser()
            if p.exists():
                return p
    return None


def main():
    ap = argparse.ArgumentParser(description="Ask Hal a question, answered from your own notes.")
    ap.add_argument("question", nargs="+")
    ap.add_argument("--k", type=int, default=8, help="passages to retrieve (default 8)")
    ap.add_argument("--cap", type=int, default=6, help="max passages fed to the model")
    ap.add_argument("--vault", default=None)
    ap.add_argument("--embed-model", default=R.EMBED_MODEL)
    ap.add_argument("--model", default=CHAT_MODEL)
    ap.add_argument("--answerer", choices=("ollama", "claude"), default=ANSWERER,
                    help="who writes the answer (retrieval is local either way)")
    ap.add_argument("--show-context", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="retrieval only, no model call")
    ap.add_argument("--mock", action="store_true", help="offline wiring test")
    args = ap.parse_args()

    vault = resolve_vault(args.vault)
    if not vault:
        err("I don't know where your vault is. Set HAL_VAULT or pass --vault.")
        sys.exit(1)

    question = " ".join(args.question)
    try:
        reply, used = answer(vault, question, k=args.k, ctx_cap=args.cap, mock=args.mock,
                             no_llm=args.no_llm, embed_model=args.embed_model, model=args.model,
                             answerer=args.answerer)
    except Exception as e:  # noqa: BLE001
        err(f"{e}")
        # Only nudge about embeddings when that's plausibly what broke — an answerer or
        # config error has nothing to do with the embedding model.
        if not args.mock and "embed" in str(e).lower():
            err("If embeddings failed, check the model:  ollama pull " + args.embed_model)
        sys.exit(1)

    if args.show_context and used and not (args.no_llm or args.mock):
        hal("Retrieved passages:")
        for h in used:
            print(f"{DIM}  • {h['title']}  ({h.get('section') or h.get('course','')})  "
                  f"score={h['score']:.3f}{OFF}")
        print()
    print(reply)


if __name__ == "__main__":
    main()

---
description: Answer a question using ONLY your own notes, with [[wikilink]] citations
argument-hint: "\"your question\"  [--k N] [--show-context]"
allowed-tools: Bash, Read
---

# /ask — Chat with your brain

Answer a question grounded in the user's own vault. Embeds the question, retrieves the most
relevant passages from the local semantic index, and asks the configured model to answer
**using only those passages**, citing each note it leaned on as an Obsidian `[[wikilink]]`. If
the notes don't cover it, it says so rather than inventing an answer. Retrieval always runs on
the machine; on the `cloud` profile the answer itself is written by Claude Code.

## Step 1 — Make sure the index exists
The answer is only as good as the index. If the user has never run it (or has added notes since),
refresh it first — it's incremental, so a re-run is near-instant:

```bash
python3 "$HAL_ROOT/vault-index.py" --vault "$HAL_VAULT"
```

(First build on a big vault does real work; every run after only re-embeds changed notes.)

## Step 2 — Ask
```bash
python3 "$HAL_ROOT/ask.py" --vault "$HAL_VAULT" $ARGUMENTS
```

If `$HAL_ROOT` isn't set, it's the repo root (the folder holding `hal`, `ask.py`, `vault-index.py`).
Useful flags:
- `--k N` — retrieve N passages (default 8); raise it for broad "compare/contrast" questions.
- `--show-context` — also print which passages were used and their similarity scores.
- `--no-llm` — show the retrieved passages only, no model call (fast sanity check).
- `--mock` — offline wiring test (deterministic embeddings, no model).

## Step 3 — Present the answer
Show the model's answer verbatim — it already contains inline `[[wikilinks]]` to the notes it
used, so the user can click straight through in Obsidian. Don't add outside knowledge on top; the
whole point is a citation-grounded answer from *their* notes. If the script reports it has no
index yet, run Step 1 and retry.

## Also available as an MCP tool
The same retrieval core is exposed by the **hal** MCP server (`hal-mcp.py`) as `search_vault`,
`ask_vault` and `get_note`, so any MCP client can query the brain. See SETUP.md → "Ask your notes
(local RAG)".

#!/usr/bin/env bash
#
# Hal — local AI assistant setup (Ubuntu / Linux)
# Installs Ollama + Qwen3-Coder-30B, Claude Code, uv, the slash-command skills,
# and wires Claude Code to your local model. Safe to re-run (idempotent).
#
set -uo pipefail

# --- Resolve paths (works no matter where this repo lives on your machine) ----
HAL_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VAULT="$HAL_ROOT/Vault"

# --- Config ------------------------------------------------------------------
MAIN_BASE="qwen3-coder:30b"      # base model pulled from Ollama
MAIN_MODEL="qwen3-coder-hal"     # custom model we build (32K context)
FAST_MODEL="llama3.2:3b"         # small model for Claude Code background tasks
EMBED_MODEL="nomic-embed-text"   # local embedding model for the RAG index ('hal ask')
NUM_CTX=32768                    # context window (fits a 3090's 24GB with q8 KV cache)

# --- Pretty output -----------------------------------------------------------
c_blue=$'\033[1;36m'; c_yellow=$'\033[1;33m'; c_green=$'\033[1;32m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '\n%s==> %s%s\n' "$c_blue" "$*" "$c_off"; }
ok()   { printf '%s  ✓ %s%s\n' "$c_green" "$*" "$c_off"; }
warn() { printf '%s  ! %s%s\n' "$c_yellow" "$*" "$c_off"; }
err()  { printf '%s  ✗ %s%s\n' "$c_red" "$*" "$c_off"; }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then have sudo && SUDO="sudo"; fi

printf '%s\n' "============================================================"
printf '   Hal — local assistant setup\n'
printf '   Root:  %s\n' "$HAL_ROOT"
printf '   Vault: %s\n' "$VAULT"
printf '%s\n' "============================================================"

# --- 0. Sanity ---------------------------------------------------------------
case "$(uname -s)" in
  Linux) ok "Linux detected" ;;
  *) warn "This script targets Linux/Ubuntu. Other OSes may need manual steps." ;;
esac
if ! have curl; then
  log "Installing curl"
  $SUDO apt-get update -y && $SUDO apt-get install -y curl || { err "Install curl manually, then re-run."; exit 1; }
fi

# --- 1. Ollama ---------------------------------------------------------------
if have ollama; then
  ok "Ollama already installed ($(ollama --version 2>/dev/null | head -n1))"
else
  log "Installing Ollama"
  curl -fsSL https://ollama.com/install.sh | sh && ok "Ollama installed" || { err "Ollama install failed"; exit 1; }
fi

# Make sure the Ollama server is responding
log "Checking Ollama server"
if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
  if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    $SUDO systemctl enable --now ollama 2>/dev/null || true
  fi
  # Fallback: start in background
  if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
    nohup ollama serve >/tmp/ollama-hal.log 2>&1 &
    sleep 3
  fi
fi
if curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then ok "Ollama server is up"; else warn "Ollama server not responding yet — it may need a moment, or start it with 'ollama serve'"; fi

# --- 2. Tune Ollama for the RTX 3090 (optional, needs sudo + systemd) --------
if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
  log "Tuning Ollama for the RTX 3090 (flash attention + q8 KV cache)"
  if $SUDO mkdir -p /etc/systemd/system/ollama.service.d 2>/dev/null; then
    $SUDO tee /etc/systemd/system/ollama.service.d/hal.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=30m"
EOF
    $SUDO systemctl daemon-reload && $SUDO systemctl restart ollama && ok "Ollama tuned & restarted" || warn "Could not restart ollama service (tuning skipped)"
    sleep 2
  else
    warn "No sudo — skipping Ollama tuning (not required, just a small VRAM optimisation)"
  fi
else
  warn "Ollama not running under systemd — skipping service tuning (not required)"
fi

# --- 3. Pull models ----------------------------------------------------------
log "Pulling main coding model: $MAIN_BASE  (~18GB — this is the long part)"
ollama pull "$MAIN_BASE" && ok "Pulled $MAIN_BASE" || { err "Failed to pull $MAIN_BASE"; exit 1; }

log "Pulling small/fast model: $FAST_MODEL  (~2GB)"
ollama pull "$FAST_MODEL" && ok "Pulled $FAST_MODEL" || warn "Failed to pull $FAST_MODEL (background tasks will be slower)"

log "Pulling embedding model: $EMBED_MODEL  (~270MB — powers 'hal ask' / RAG)"
ollama pull "$EMBED_MODEL" && ok "Pulled $EMBED_MODEL" || warn "Failed to pull $EMBED_MODEL — 'hal ask' won't work until you run: ollama pull $EMBED_MODEL"

# --- 4. Build the 32K-context model -----------------------------------------
log "Building custom model '$MAIN_MODEL' with ${NUM_CTX}-token context"
MODELFILE="$(mktemp)"
cat > "$MODELFILE" <<EOF
FROM $MAIN_BASE
PARAMETER num_ctx $NUM_CTX
EOF
ollama create "$MAIN_MODEL" -f "$MODELFILE" && ok "Created $MAIN_MODEL" || warn "Could not create $MAIN_MODEL — Hal will fall back to $MAIN_BASE"
rm -f "$MODELFILE"

# --- 5. Node.js + Claude Code ------------------------------------------------
NODE_OK=0
if have node; then
  NODE_MAJOR="$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')"
  [ "${NODE_MAJOR:-0}" -ge 18 ] && NODE_OK=1
fi
if [ "$NODE_OK" -ne 1 ]; then
  log "Installing Node.js 20 LTS"
  if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO bash - && $SUDO apt-get install -y nodejs && NODE_OK=1
  fi
  have node && [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -ge 18 ] && NODE_OK=1
fi
if [ "$NODE_OK" -eq 1 ]; then ok "Node.js $(node -v) ready"; else err "Node.js 18+ required. Install from https://nodejs.org then re-run."; fi

# Use a user-local npm global prefix so we never need sudo for global installs
log "Configuring npm (user-local global prefix, no sudo needed)"
mkdir -p "$HOME/.npm-global"
npm config set prefix "$HOME/.npm-global" >/dev/null 2>&1 || true
export PATH="$HOME/.npm-global/bin:$PATH"

if have claude; then
  ok "Claude Code already installed ($(claude --version 2>/dev/null | head -n1))"
else
  log "Installing Claude Code"
  npm install -g @anthropic-ai/claude-code && ok "Claude Code installed" || warn "Claude Code install failed — try: npm install -g @anthropic-ai/claude-code"
fi

# --- 6. uv (for the Obsidian MCP server via uvx) -----------------------------
if have uvx || have uv; then
  ok "uv already installed"
else
  log "Installing uv (runs the Obsidian MCP server)"
  curl -LsSf https://astral.sh/uv/install.sh | sh && ok "uv installed" || warn "uv install failed — Obsidian MCP needs it; install from https://astral.sh/uv"
fi

# --- 6b. PDF tooling (read & summarise lecture slides locally) ---------------
log "Installing PDF text-extraction tooling (for lecture slides)"
# The local model is text-only, so we extract text from slide PDFs before summarising.
if have pdftotext; then
  ok "poppler-utils (pdftotext) already present"
elif [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
  $SUDO apt-get update -y >/dev/null 2>&1
  $SUDO apt-get install -y poppler-utils && ok "Installed poppler-utils (pdftotext/pdftoppm)" \
    || warn "Could not install poppler-utils — install it for PDF text extraction"
else
  warn "No sudo — install poppler-utils manually: sudo apt-get install poppler-utils"
fi
# Optional OCR for scanned/image-only slides
if have tesseract; then
  ok "tesseract OCR present (scanned slides supported)"
elif [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
  $SUDO apt-get install -y tesseract-ocr >/dev/null 2>&1 && ok "Installed tesseract OCR" \
    || warn "tesseract not installed (only needed for scanned/image-only PDFs)"
fi
# pandoc reads .docx / .html (used by extract-docx.sh and extract-html.sh)
if have pandoc; then
  ok "pandoc present (DOCX/HTML extraction)"
elif [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
  $SUDO apt-get install -y pandoc >/dev/null 2>&1 && ok "Installed pandoc (DOCX/HTML extraction)" \
    || warn "pandoc not installed — DOCX/HTML extraction will fall back to Python"
else
  warn "No sudo — install pandoc manually for DOCX/HTML: sudo apt-get install pandoc"
fi
# Python extractors: pymupdf (PDF fallback), python-docx (DOCX fallback),
# python-pptx (PPTX — pandoc cannot read .pptx, so this one is required for slides-as-pptx)
if have pip3 || have pip; then
  for pkg in pymupdf python-docx python-pptx; do
    (pip3 install --user --quiet "$pkg" 2>/dev/null || pip install --user --quiet --break-system-packages "$pkg" 2>/dev/null) \
      && ok "$pkg available" || warn "$pkg not installed (needed for some document types)"
  done
fi

# --- 7. Install slash-command skills ----------------------------------------
log "Installing slash-command skills to ~/.claude/commands/"
mkdir -p "$HOME/.claude/commands"
cp -f "$HAL_ROOT"/claude-commands/*.md "$HOME/.claude/commands/" && \
  ok "Installed: $(cd "$HAL_ROOT/claude-commands" && ls *.md | sed 's/\.md//' | sed 's/^/\//' | tr '\n' ' ')" || \
  warn "Could not copy command files"

# --- 8. Ensure the MCP servers are registered in the vault .mcp.json ----------
# Two local servers: 'obsidian' (live vault via the Local REST API) and 'hal' (your own
# RAG brain — search_vault / ask_vault / get_note from hal-mcp.py). This is MERGE-SAFE:
# it preserves any servers you've added yourself and just guarantees these two exist, with
# the hal server's script path baked to an absolute path so any MCP client can launch it.
log "Registering MCP servers in $VAULT/.mcp.json (obsidian + hal)"
HAL_MCP="$HAL_ROOT/hal-mcp.py"
if have python3; then
  HAL_ROOT="$HAL_ROOT" VAULT="$VAULT" HAL_MCP="$HAL_MCP" \
  MAIN_MODEL="$MAIN_MODEL" EMBED_MODEL="$EMBED_MODEL" \
  python3 - "$VAULT/.mcp.json" <<'PY' && ok "MCP servers registered (obsidian + hal)" || warn "Could not update .mcp.json — add the 'hal' server by hand (see SETUP.md)"
import json, os, sys, pathlib
p = pathlib.Path(sys.argv[1])
cfg = {}
if p.exists():
    try:
        cfg = json.loads(p.read_text())
    except Exception:
        cfg = {}
servers = cfg.setdefault("mcpServers", {})
servers.setdefault("obsidian", {
    "command": "uvx",
    "args": ["mcp-obsidian"],
    "env": {"OBSIDIAN_API_KEY": "${OBSIDIAN_API_KEY}",
            "OBSIDIAN_HOST": "127.0.0.1", "OBSIDIAN_PORT": "27124"},
})
# Always (re)write the hal entry so the baked script path stays correct if the repo moves.
servers["hal"] = {
    "command": "uv",
    "args": ["run", "--with", "mcp", os.environ["HAL_MCP"]],
    "env": {
        "HAL_VAULT": os.environ["VAULT"],
        "HAL_MODEL": os.environ["MAIN_MODEL"],
        "HAL_EMBED_MODEL": os.environ["EMBED_MODEL"],
        "ANTHROPIC_BASE_URL": "http://localhost:11434",
    },
}
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY
else
  warn "python3 not found — cannot update .mcp.json automatically (add the 'hal' server by hand)"
fi

# --- 9. Make 'hal' available on PATH + set shell env -------------------------
log "Linking the 'hal' launcher and updating your shell profile"
chmod +x "$HAL_ROOT/hal" "$HAL_ROOT"/*.sh 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sf "$HAL_ROOT/hal" "$HOME/.local/bin/hal"

PROFILE="$HOME/.bashrc"
MARK_BEGIN="# >>> hal setup >>>"
MARK_END="# <<< hal setup <<<"
# Remove any previous managed block, then append a fresh one
if [ -f "$PROFILE" ]; then
  sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$PROFILE"
fi
cat >> "$PROFILE" <<EOF
$MARK_BEGIN
export HAL_VAULT="$VAULT"
export PATH="\$HOME/.local/bin:\$HOME/.npm-global/bin:\$PATH"
$MARK_END
EOF
ok "Updated $PROFILE (HAL_VAULT + PATH)"

# --- Done --------------------------------------------------------------------
cat <<EOF

${c_green}============================================================${c_off}
${c_green}  Hal install complete.${c_off}
${c_green}============================================================${c_off}

Next steps (a few one-time manual bits):

  1) Open a NEW terminal (so the PATH changes take effect), or run:
       source ~/.bashrc

  2) In Obsidian:
       - Settings → Community plugins → turn on Community plugins
       - Browse & install:  "Local REST API"   and   "Dataview"
       - Open "Local REST API" settings, copy the API key
       - File → Open vault → "Open folder as vault" → choose:
           $VAULT

  3) Give Hal the Obsidian key (one time):
       hal obsidian-key <PASTE_KEY_HERE>

  4) Launch it:
       hal            # opens Claude Code in your vault, using the local model
     then type:
       /resume

Tip: run 'hal here' inside any code project to use Hal there instead of the vault.
See SETUP.md for details and troubleshooting.
EOF

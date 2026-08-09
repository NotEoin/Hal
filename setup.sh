#!/usr/bin/env bash
#
# Hal — local AI assistant setup (Ubuntu / Linux)
# Installs Claude Code, uv, the slash-command skills, and (for the local profiles)
# Ollama + a local model. Safe to re-run (idempotent).
#
# Hal runs in one of three profiles. By default this script reads your GPU and proposes
# the one that fits; you confirm, or override:
#
#   ./setup.sh                    detect VRAM, propose a profile, ask
#   ./setup.sh --profile 24gb     force a profile: 24gb | 12gb | cloud
#   ./setup.sh --yes              accept the detected profile without prompting
#   ./setup.sh --profile cloud -y  fully non-interactive
#   ./setup.sh --university "My University"   name the root of the study hierarchy
#
#   24gb   Qwen3-Coder-30B-A3B locally. Best local quality; needs ~20GB of VRAM.
#   12gb   Qwen3.5-9B locally. Fits a 4070/3060-class card, still fully private.
#   cloud  No local LLM at all — plain Claude Code with your own login. Any machine.
#          Note retrieval for `hal ask` stays local even here (see EMBED_MODEL below).
#
set -uo pipefail

# --- Resolve paths (works no matter where this repo lives on your machine) ----
HAL_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VAULT="$HAL_ROOT/Vault"
CONF_FILE="$HAL_ROOT/hal.conf"   # git-ignored; written here, sourced by ./hal

# --- Args --------------------------------------------------------------------
PROFILE=""
ASSUME_YES=0
UNIVERSITY_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 || true ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --university) UNIVERSITY_NAME="${2:-}"; shift 2 || true ;;
    --university=*) UNIVERSITY_NAME="${1#*=}"; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

# --- Config ------------------------------------------------------------------
# The embedding model is pulled in EVERY profile, cloud included. It is small (~270MB),
# runs happily on the CPU, and keeps `hal ask` retrieval on your own machine.
EMBED_MODEL="nomic-embed-text"
NUM_CTX=32768                    # context window for the local profiles (q8 KV cache)
# Per-profile model choice is set by pick_profile() below.
MAIN_BASE=""; MAIN_MODEL=""; FAST_MODEL=""; ANSWERER=""
# Names the root note of the study hierarchy that `hal summarize` builds, and the first
# hop of every breadcrumb. Reused from an existing hal.conf so re-running never renames
# a spine you have already built.
if [ -z "$UNIVERSITY_NAME" ] && [ -f "$CONF_FILE" ]; then
  UNIVERSITY_NAME="$(. "$CONF_FILE" 2>/dev/null; printf %s "${HAL_UNIVERSITY:-}")"
fi
UNIVERSITY_NAME="${UNIVERSITY_NAME:-University}"

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

# --- 0a. Choose a profile ----------------------------------------------------
# Total VRAM in MiB, or empty when there's no usable NVIDIA GPU.
detect_vram_mib() {
  have nvidia-smi || return 0
  nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
    | sort -nr | head -n1
}

# Map VRAM to a profile. The thresholds sit below the nominal card sizes because
# vendors' "24GB" is 24564 MiB and a desktop already holds ~1GB of it.
profile_for_vram() {
  local mib="${1:-0}"
  if   [ "$mib" -ge 22000 ]; then echo "24gb"
  elif [ "$mib" -ge 11000 ]; then echo "12gb"
  else echo "cloud"; fi
}

# Fill in the model choices for the selected profile.
pick_profile() {
  case "$1" in
    24gb)
      MAIN_BASE="qwen3-coder:30b"; MAIN_MODEL="qwen3-coder-hal"
      FAST_MODEL="llama3.2:3b";    ANSWERER="ollama" ;;
    12gb)
      # Qwen3-Coder has no variant below 30B, so the 12GB profile changes family rather
      # than shrinking. Qwen3.5-9B is dense and tool-call trained, which is what Claude
      # Code actually leans on, and it leaves GPU headroom for hal-voice's TTS engine.
      MAIN_BASE="qwen3.5:9b";      MAIN_MODEL="qwen3.5-hal"
      FAST_MODEL="qwen3.5:0.8b";   ANSWERER="ollama" ;;
    cloud)
      MAIN_BASE=""; MAIN_MODEL=""; FAST_MODEL=""; ANSWERER="claude" ;;
    *) err "Unknown profile: $1 (expected 24gb, 12gb or cloud)"; exit 1 ;;
  esac
}

VRAM_MIB="$(detect_vram_mib)"
if [ -n "$PROFILE" ]; then
  log "Profile: $PROFILE (forced with --profile)"
else
  if [ -n "$VRAM_MIB" ]; then
    PROFILE="$(profile_for_vram "$VRAM_MIB")"
    log "Detected $(( VRAM_MIB / 1024 ))GB of VRAM → suggesting the '$PROFILE' profile"
  else
    PROFILE="cloud"
    log "No NVIDIA GPU detected → suggesting the 'cloud' profile"
  fi
  if [ "$ASSUME_YES" -ne 1 ]; then
    printf '\n  24gb   local Qwen3-Coder-30B      (needs ~20GB VRAM)\n'
    printf '  12gb   local Qwen3.5-9B           (needs ~8GB VRAM)\n'
    printf '  cloud  no local LLM, Claude Code  (any machine)\n\n'
    printf 'Profile [%s]: ' "$PROFILE"
    read -r reply || reply=""
    [ -n "$reply" ] && PROFILE="$reply"
  fi
fi
pick_profile "$PROFILE"
ok "Using profile: $PROFILE"

# Everything Ollama-related is skipped entirely in the cloud profile EXCEPT the small
# embedding model, which keeps `hal ask` retrieval local.
NEEDS_LOCAL_LLM=1
[ "$PROFILE" = "cloud" ] && NEEDS_LOCAL_LLM=0

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
# Installed in every profile. In `cloud` it serves ONLY the ~270MB embedding model, so
# that `hal ask` can search your notes without shipping them anywhere.
if [ "$NEEDS_LOCAL_LLM" -eq 0 ]; then
  log "Cloud profile: Ollama is still needed, but only for local embeddings (~270MB)"
fi
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

# --- 2. Tune Ollama for your GPU (optional, needs sudo + systemd) ------------
# Flash attention + a q8_0 KV cache are what make a 32K window affordable. KEEP_ALIVE is
# shorter on 12GB: an idle model squatting in VRAM there starves hal-voice's TTS engine.
if [ "$NEEDS_LOCAL_LLM" -eq 1 ] && have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
  KEEP_ALIVE="30m"; [ "$PROFILE" = "12gb" ] && KEEP_ALIVE="5m"
  log "Tuning Ollama for the '$PROFILE' profile (flash attention, q8 KV cache, keep-alive $KEEP_ALIVE)"
  if $SUDO mkdir -p /etc/systemd/system/ollama.service.d 2>/dev/null; then
    $SUDO tee /etc/systemd/system/ollama.service.d/hal.conf >/dev/null <<EOF
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=$KEEP_ALIVE"
EOF
    $SUDO systemctl daemon-reload && $SUDO systemctl restart ollama && ok "Ollama tuned & restarted" || warn "Could not restart ollama service (tuning skipped)"
    sleep 2
  else
    warn "No sudo — skipping Ollama tuning (not required, just a small VRAM optimisation)"
  fi
elif [ "$NEEDS_LOCAL_LLM" -eq 1 ]; then
  warn "Ollama not running under systemd — skipping service tuning (not required)"
fi

# --- 3. Pull models ----------------------------------------------------------
if [ "$NEEDS_LOCAL_LLM" -eq 1 ]; then
  log "Pulling main model: $MAIN_BASE  (this is the long part)"
  ollama pull "$MAIN_BASE" && ok "Pulled $MAIN_BASE" || { err "Failed to pull $MAIN_BASE"; exit 1; }

  log "Pulling small/fast model: $FAST_MODEL"
  ollama pull "$FAST_MODEL" && ok "Pulled $FAST_MODEL" || warn "Failed to pull $FAST_MODEL (background tasks will be slower)"
else
  ok "Cloud profile — skipping the local LLM (Claude Code will use your own login)"
fi

log "Pulling embedding model: $EMBED_MODEL  (~270MB — powers 'hal ask' / RAG)"
ollama pull "$EMBED_MODEL" && ok "Pulled $EMBED_MODEL" || warn "Failed to pull $EMBED_MODEL — 'hal ask' won't work until you run: ollama pull $EMBED_MODEL"

# --- 4. Build the 32K-context model -----------------------------------------
if [ "$NEEDS_LOCAL_LLM" -eq 1 ]; then
log "Building custom model '$MAIN_MODEL' with ${NUM_CTX}-token context"
MODELFILE="$(mktemp)"
cat > "$MODELFILE" <<EOF
FROM $MAIN_BASE
PARAMETER num_ctx $NUM_CTX
EOF
ollama create "$MAIN_MODEL" -f "$MODELFILE" && ok "Created $MAIN_MODEL" || warn "Could not create $MAIN_MODEL — Hal will fall back to $MAIN_BASE"
rm -f "$MODELFILE"
fi

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
  MAIN_MODEL="$MAIN_MODEL" EMBED_MODEL="$EMBED_MODEL" ANSWERER="$ANSWERER" \
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
        "HAL_MODEL": os.environ["MAIN_MODEL"],          # empty in the cloud profile
        "HAL_EMBED_MODEL": os.environ["EMBED_MODEL"],
        "HAL_ANSWERER": os.environ["ANSWERER"],
        # Embeddings are always local. Kept separate from ANTHROPIC_BASE_URL so the cloud
        # profile can point Claude Code at Anthropic while retrieval stays on this machine.
        "HAL_OLLAMA_URL": "http://localhost:11434",
    },
}
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY
else
  warn "python3 not found — cannot update .mcp.json automatically (add the 'hal' server by hand)"
fi

# --- 9. Write hal.conf (the profile the launcher reads) ----------------------
# One git-ignored file is the single source of truth for "which brain am I using".
# ./hal sources it, and hal-voice can import it too so the two never drift apart.
log "Writing $CONF_FILE"
cat > "$CONF_FILE" <<EOF
# Hal — resolved setup profile. Generated by setup.sh on $(date -u +%Y-%m-%dT%H:%MZ).
# Safe to edit by hand; re-running setup.sh overwrites it. Git-ignored on purpose:
# it records choices about YOUR machine, not about the project.

# Which profile this install uses: 24gb | 12gb | cloud
HAL_PROFILE="$PROFILE"

# The local model Claude Code talks to. Empty in the cloud profile, which means
# "don't redirect Claude Code at all — use its own login".
HAL_MODEL="$MAIN_MODEL"
HAL_FAST_MODEL="$FAST_MODEL"

# Context window the custom model was built with (see NUM_CTX in setup.sh).
HAL_NUM_CTX="$NUM_CTX"

# Retrieval for 'hal ask' / 'hal index'. Always local, in every profile.
HAL_EMBED_MODEL="$EMBED_MODEL"
HAL_OLLAMA_URL="http://localhost:11434"

# Who writes the final answer for 'hal ask': ollama | claude
HAL_ANSWERER="$ANSWERER"

# Names the root note of the study hierarchy, and the first hop of every breadcrumb
# written by 'hal summarize'. Change it to your own institution.
HAL_UNIVERSITY="$UNIVERSITY_NAME"
EOF
ok "Profile '$PROFILE' saved to hal.conf"

# --- 10. Make 'hal' available on PATH + set shell env ------------------------
log "Linking the 'hal' launcher and updating your shell profile"
chmod +x "$HAL_ROOT/hal" "$HAL_ROOT"/*.sh 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sf "$HAL_ROOT/hal" "$HOME/.local/bin/hal"

# NB: not $PROFILE — that holds the setup profile (24gb/12gb/cloud).
SHELL_PROFILE="$HOME/.bashrc"
MARK_BEGIN="# >>> hal setup >>>"
MARK_END="# <<< hal setup <<<"
# Remove any previous managed block, then append a fresh one
if [ -f "$SHELL_PROFILE" ]; then
  sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$SHELL_PROFILE"
fi
cat >> "$SHELL_PROFILE" <<EOF
$MARK_BEGIN
export HAL_VAULT="$VAULT"
export PATH="\$HOME/.local/bin:\$HOME/.npm-global/bin:\$PATH"
$MARK_END
EOF
ok "Updated $SHELL_PROFILE (HAL_VAULT + PATH)"

# --- Done --------------------------------------------------------------------
cat <<EOF

${c_green}============================================================${c_off}
${c_green}  Hal install complete — profile: $PROFILE${c_off}
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
       hal            # opens Claude Code in your vault
     then type:
       /resume

Profile '$PROFILE' is recorded in hal.conf — edit it, or re-run ./setup.sh --profile <name>
to switch between 24gb, 12gb and cloud.

Tip: run 'hal here' inside any code project to use Hal there instead of the vault.
See SETUP.md for details and troubleshooting.
EOF

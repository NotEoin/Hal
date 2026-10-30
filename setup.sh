#!/usr/bin/env bash
#
# Hal — local AI setup (Ubuntu / Linux)
# Installs Ollama and the local models for your GPU, Claude Code, and the `hal` launcher,
# and writes ~/.config/hal/config.toml. Safe to re-run (idempotent). Extensions (memory,
# calendar, voice, ...) are added afterwards with `hal ext add` or ../../install.sh.
#
# Hal runs in one of three profiles. By default this script reads your GPU and proposes
# the one that fits; you confirm, or override:
#
#   ./setup.sh                    detect VRAM, propose a profile, ask
#   ./setup.sh --profile 24gb     force a profile: max | 24gb | 12gb | cloud
#   ./setup.sh --yes              accept the detected profile without prompting
#   ./setup.sh --profile cloud -y  fully non-interactive
#   ./setup.sh --models-dir /mnt/big/hal-models   where the max profile keeps its GGUFs
#
#   max    Three models through llama.cpp, five modes (see MODES.md). 24GB VRAM + 64GB RAM.
#   24gb   Qwen3.8-27B locally. Best local quality; needs ~20GB of VRAM.
#   12gb   Gemma 4 12B locally. Fits a 4070/3060-class card, still fully private.
#   cloud  No local coding model — plain Claude Code with your own login. Any machine.
#          Hal's chat brain still runs locally, on a small model.
#
set -uo pipefail

# --- Resolve paths (works no matter where this repo lives on your machine) ----
HAL_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CONF_FILE="$HAL_ROOT/hal.conf"   # git-ignored; written here, sourced by ./hal

# --- Args --------------------------------------------------------------------
PROFILE=""
ASSUME_YES=0
MODELS_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 || true ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --models-dir) MODELS_DIR="${2:-}"; shift 2 || true ;;
    --models-dir=*) MODELS_DIR="${1#*=}"; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

# --- Config ------------------------------------------------------------------
NUM_CTX=32768                    # context window for the local profiles (q8 KV cache)
# Per-profile model choice is set by pick_profile() below.
MAIN_BASE=""; MAIN_MODEL=""; FAST_MODEL=""
# Hal's chat brain (hal chat, the voice): a model small enough to sit beside the voice engine,
# chosen per profile with `hal eval`. Change it later with [hal].chat_model in the config.
BRAIN_MODEL=""
# The max profile's GGUFs (about 160GB), reused from an existing hal.conf like the above
if [ -z "$MODELS_DIR" ] && [ -f "$CONF_FILE" ]; then
  MODELS_DIR="$(. "$CONF_FILE" 2>/dev/null; printf %s "${HAL_MODELS_DIR:-}")"
fi
MODELS_DIR="${MODELS_DIR:-$HAL_ROOT/models}"
# Pinned so the mode presets in modes/ always match the server's flags
LLAMA_CPP_BUILD="b11512"
LLAMA_CPP_DIR="$HOME/.local/share/hal/llama.cpp/llama-$LLAMA_CPP_BUILD"

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
printf '%s\n' "============================================================"

# --- 0a. Choose a profile ----------------------------------------------------
# Total VRAM in MiB, or empty when there's no usable NVIDIA GPU.
detect_vram_mib() {
  have nvidia-smi || return 0
  nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
    | sort -nr | head -n1
}

# Total system RAM in GiB.
detect_ram_gib() {
  awk '/^MemTotal:/ { printf "%d", $2 / 1048576 }' /proc/meminfo 2>/dev/null
}

# Map VRAM (MiB) and RAM (GiB) to a profile. The thresholds sit below the nominal sizes
# because vendors' "24GB" is 24564 MiB and a desktop already holds ~1GB of it.
profile_for_hardware() {
  local mib="${1:-0}" ram="${2:-0}"
  if   [ "$mib" -ge 22000 ] && [ "$ram" -ge 60 ]; then echo "max"
  elif [ "$mib" -ge 22000 ]; then echo "24gb"
  elif [ "$mib" -ge 11000 ]; then echo "12gb"
  else echo "cloud"; fi
}

# Fill in the model choices for the selected profile.
pick_profile() {
  case "$1" in
    max)
      # Claude Code talks to the llama.cpp router; see modes/ for the models
      MAIN_BASE=""; MAIN_MODEL="hal-gpu"; FAST_MODEL="hal-gpu"; BRAIN_MODEL="gemma4:12b-it-qat" ;;
    24gb)
      MAIN_BASE="qwen3.8:27b";     MAIN_MODEL="qwen3.8-hal"
      FAST_MODEL="qwen3.5:4b";     BRAIN_MODEL="gemma4:12b-it-qat" ;;
    12gb)
      # Gemma 4 12B (quantisation-aware trained) is reliable at tool calls, which is
      # what Claude Code leans on, and leaves GPU headroom for hal-voice's TTS engine.
      MAIN_BASE="gemma4:12b-it-qat"; MAIN_MODEL="gemma4-hal"
      FAST_MODEL="qwen3.5:2b";       BRAIN_MODEL="qwen3.5:4b" ;;
    cloud)
      MAIN_BASE=""; MAIN_MODEL=""; FAST_MODEL=""; BRAIN_MODEL="qwen3.5:2b" ;;
    *) err "Unknown profile: $1 (expected max, 24gb, 12gb or cloud)"; exit 1 ;;
  esac
}

VRAM_MIB="$(detect_vram_mib)"
RAM_GIB="$(detect_ram_gib)"
if [ -n "$PROFILE" ]; then
  log "Profile: $PROFILE (forced with --profile)"
else
  if [ -n "$VRAM_MIB" ]; then
    PROFILE="$(profile_for_hardware "$VRAM_MIB" "$RAM_GIB")"
    log "Detected $(( VRAM_MIB / 1024 ))GB of VRAM and ${RAM_GIB}GB of RAM → suggesting the '$PROFILE' profile"
  else
    PROFILE="cloud"
    log "No NVIDIA GPU detected → suggesting the 'cloud' profile"
  fi
  if [ "$ASSUME_YES" -ne 1 ]; then
    printf '\n  max    three models, five modes   (needs 24GB VRAM + 64GB RAM, ~160GB disk)\n'
    printf '  24gb   local Qwen3.8-27B          (needs ~20GB VRAM)\n'
    printf '  12gb   local Gemma 4 12B          (needs ~8GB VRAM)\n'
    printf '  cloud  no local LLM, Claude Code  (any machine)\n\n'
    printf 'Profile [%s]: ' "$PROFILE"
    read -r reply || reply=""
    [ -n "$reply" ] && PROFILE="$reply"
  fi
fi
pick_profile "$PROFILE"
ok "Using profile: $PROFILE"

# Ollama serves a chat model only on 24gb and 12gb. The max profile serves its models
# through llama.cpp, and cloud has none; both still pull the small embedding model,
# which keeps `hal ask` retrieval local.
NEEDS_LOCAL_LLM=1
case "$PROFILE" in cloud|max) NEEDS_LOCAL_LLM=0 ;; esac

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
# Installed in every profile: in `cloud` it serves only Hal's small chat brain (and the
# memory extension's embeddings), so your conversations and notes stay on this machine.
if [ "$NEEDS_LOCAL_LLM" -eq 0 ]; then
  log "Profile $PROFILE: Ollama is still needed, but only for Hal's small chat model"
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
elif [ "$PROFILE" = "max" ]; then
  ok "Profile max: the models come from Hugging Face in the next step, not from Ollama"
else
  ok "Cloud profile — skipping the local LLM (Claude Code will use your own login)"
fi

log "Pulling Hal's chat brain: $BRAIN_MODEL  (answers, and calls the extensions' tools)"
ollama pull "$BRAIN_MODEL" && ok "Pulled $BRAIN_MODEL" || warn "Failed to pull $BRAIN_MODEL — 'hal chat' won't work until you run: ollama pull $BRAIN_MODEL"

# --- 3b. llama.cpp and the max profile's models -----------------------------
# One release build of llama-server (CUDA 12.8, which any 12.8+ driver runs) plus the
# GGUFs named in modes/*.ini. curl -C - resumes a partial download on re-run.
MAX_MODEL_FILES=(
  "unsloth/Qwen3.8-27B-GGUF qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf Qwen3.8-27B-UD-Q4_K_XL.gguf"
  "unsloth/Qwen3.8-27B-GGUF qwen3.8-27b/MTP/mtp-Qwen3.8-27B-Q4_0.gguf MTP/mtp-Qwen3.8-27B-Q4_0.gguf"
  "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF qwen3.8-flash-next-coder/IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf"
  "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF qwen3.8-flash-next-coder/IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf"
  "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF qwen3.8-flash-next/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00001-of-00002.gguf"
  "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF qwen3.8-flash-next/IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf IQ3_XXS/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS-00002-of-00002.gguf"
  "unsloth/Qwen3.8-Flash-Next-GGUF qwen3.8-flash-next/mtp-Qwen3.8-Flash-Next-Q8_0.gguf mtp-Qwen3.8-Flash-Next-Q8_0.gguf"
  "ggml-org/Kev-0.8B-GGUF kev-0.8b/Kev-0.8B-Q8_0.gguf Kev-0.8B-Q8_0.gguf"
)

install_llama_cpp() {
  local base="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_CPP_BUILD" tmp a
  if [ -x "$LLAMA_CPP_DIR/llama-server" ]; then
    ok "llama.cpp $LLAMA_CPP_BUILD already installed"
    return 0
  fi
  log "Installing llama.cpp $LLAMA_CPP_BUILD (CUDA build, ~750MB with its CUDA runtime)"
  tmp="$(mktemp -d)"
  for a in "llama-$LLAMA_CPP_BUILD-bin-ubuntu-cuda-12.8-x64.tar.gz" \
           "cudart-llama-$LLAMA_CPP_BUILD-bin-ubuntu-cuda-12.8-x64.tar.gz"; do
    # REQ-SEC-78: extract only an archive whose sha256 is in the repo's lock file
    curl -fL --progress-bar -o "$tmp/$a" "$base/$a" \
      && python3 -I "$HAL_ROOT/packages/hal-gateway/src/halgateway/llamacpp.py" verify "$LLAMA_CPP_BUILD" "$tmp/$a" \
      && tar xzf "$tmp/$a" -C "$tmp" \
      || { err "Could not download or verify $a"; rm -rf "$tmp"; return 1; }
  done
  mkdir -p "$LLAMA_CPP_DIR"
  # The CUDA runtime goes beside llama-server, where its backend loader looks for it
  cp -a "$tmp/llama-$LLAMA_CPP_BUILD"/. "$LLAMA_CPP_DIR"/
  cp -a "$tmp/cudart-llama-$LLAMA_CPP_BUILD-bin-ubuntu-cuda-12.8-x64"/. "$LLAMA_CPP_DIR"/
  rm -rf "$tmp"
  "$LLAMA_CPP_DIR/llama-server" --list-devices 2>&1 | grep -q CUDA0 \
    && ok "llama.cpp installed and sees your GPU" \
    || warn "llama.cpp installed, but it doesn't see a CUDA GPU (check the NVIDIA driver)"
}

download_max_models() {
  local entry repo dest src url remote_size local_size
  log "Downloading the max profile's models into $MODELS_DIR (~160GB, the long part)"
  mkdir -p "$MODELS_DIR" || { err "Cannot create $MODELS_DIR"; return 1; }
  for entry in "${MAX_MODEL_FILES[@]}"; do
    read -r repo dest src <<< "$entry"
    url="https://huggingface.co/$repo/resolve/main/$src"
    # Resuming a file that is already complete fails with HTTP 416, so compare sizes first
    remote_size="$(curl -sIL "$url" | tr -d '\r' \
      | awk 'tolower($1) == "content-length:" { n = $2 } END { print n + 0 }')"
    local_size="$(stat -c %s "$MODELS_DIR/$dest" 2>/dev/null || echo 0)"
    if [ "$remote_size" -gt 0 ] && [ "$local_size" -eq "$remote_size" ]; then
      ok "$dest already downloaded"
      continue
    fi
    mkdir -p "$(dirname "$MODELS_DIR/$dest")"
    printf '  %s\n' "$dest"
    curl -fL -C - --progress-bar -o "$MODELS_DIR/$dest" "$url" \
      || { err "Failed to download $repo/$src (re-run setup.sh to resume)"; return 1; }
  done
  ok "Models ready in $MODELS_DIR"
}

if [ "$PROFILE" = "max" ]; then
  install_llama_cpp || exit 1
  download_max_models || exit 1
fi

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

# --- 9. Write hal.conf (the profile the launcher reads) ----------------------
# One git-ignored file is the single source of truth for "which brain am I using".
# ./hal sources it, and hal-voice can import it too so the two never drift apart.
log "Writing $CONF_FILE"
cat > "$CONF_FILE" <<EOF
# Hal — resolved setup profile. Generated by setup.sh on $(date -u +%Y-%m-%dT%H:%MZ).
# Safe to edit by hand; re-running setup.sh overwrites it. Git-ignored on purpose:
# it records choices about YOUR machine, not about the project.

# Which profile this install uses: max | 24gb | 12gb | cloud
HAL_PROFILE="$PROFILE"

# The local model Claude Code talks to. Empty in the cloud profile, which means
# "don't redirect Claude Code at all — use its own login".
HAL_MODEL="$MAIN_MODEL"
HAL_FAST_MODEL="$FAST_MODEL"

# Context window the custom model was built with (see NUM_CTX in setup.sh).
HAL_NUM_CTX="$NUM_CTX"

HAL_OLLAMA_URL="http://localhost:11434"
$(if [ "$PROFILE" = "max" ]; then cat <<MAX

# max profile: models served by a llama.cpp router, in one of five modes (see MODES.md).
# HAL_MODE is the default; 'hal mode <name>' picks another for one launch.
HAL_BACKEND="llamacpp"
HAL_MODE="gpu"
HAL_MODELS_DIR="$MODELS_DIR"
HAL_LLAMA_SERVER="$LLAMA_CPP_DIR/llama-server"
HAL_LLAMA_URL="http://127.0.0.1:8080"
MAX
fi)
EOF
ok "Profile '$PROFILE' saved to hal.conf"

log "Writing Hal's shared settings"
"$HAL_ROOT/hal" configure --chat-model "$BRAIN_MODEL" && ok "~/.config/hal/config.toml ready"

# --- 10. Make 'hal' available on PATH + set shell env ------------------------
log "Linking the 'hal' launcher and updating your shell profile"
chmod +x "$HAL_ROOT/hal" "$HAL_ROOT"/*.sh 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sf "$HAL_ROOT/hal" "$HOME/.local/bin/hal"

# NB: not $PROFILE — that holds the setup profile (max/24gb/12gb/cloud).
SHELL_PROFILE="$HOME/.bashrc"
MARK_BEGIN="# >>> hal setup >>>"
MARK_END="# <<< hal setup <<<"
# Remove any previous managed block, then append a fresh one
if [ -f "$SHELL_PROFILE" ]; then
  sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$SHELL_PROFILE"
fi
cat >> "$SHELL_PROFILE" <<EOF
$MARK_BEGIN
export PATH="\$HOME/.local/bin:\$HOME/.npm-global/bin:\$PATH"
$MARK_END
EOF
ok "Updated $SHELL_PROFILE (PATH)"

# --- Done --------------------------------------------------------------------
cat <<EOF

${c_green}============================================================${c_off}
${c_green}  Hal install complete — profile: $PROFILE${c_off}
${c_green}============================================================${c_off}

Next steps:

  1) Open a NEW terminal (so the PATH changes take effect), or run:
       source ~/.bashrc

  2) Talk to Hal:
       hal chat

  3) Add what you want Hal to do (or run ../../install.sh for the usual set):
       hal ext list
       hal ext add --pack assistant      # memory, calendar, tasks, notify, clock, web, briefing, voice

  4) Code with the local model:
       hal here        # Claude Code in the current directory

Profile '$PROFILE' is recorded in hal.conf — edit it, or re-run ./setup.sh --profile <name>
to switch between max, 24gb, 12gb and cloud.
$( [ "$PROFILE" = "max" ] && printf '\nOn max, pick a mode per launch: hal mode gpu | cpu | hybrid | hybrid-256k | combined.\n' )

See SETUP.md for details and troubleshooting.
EOF

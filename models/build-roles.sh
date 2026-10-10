#!/usr/bin/env bash
# Builds Hal's role models from catalog.txt and points hal.conf at them.
#
#   hal-coder   qwen3.8:27b         main model: coding, agents, research
#   hal-writer  gemma4:31b-it-qat   prose people read: letters, emails
#   hal-fast    qwen3.5:4b          triage, classification, JSON
#
#   ./models/build-roles.sh
set -euo pipefail

HAL_ROOT="${HAL_ROOT:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
# Who hal-writer writes for, in its system prompt.
WRITER_FOR="${HAL_WRITER_FOR:-the owner of this computer}"
CODER_CTX="${CODER_CTX:-65536}"

curl -fsS http://localhost:11434/api/version > /dev/null || {
  echo "Ollama is not running: sudo systemctl start ollama" >&2; exit 1; }

"$(dirname "$0")/pull-all.sh" --roles || {
  echo "Role models failed to download; see ~/.local/state/hal/pull-all.log" >&2
  exit 1; }

build() {
  local file
  file="$(mktemp)"
  printf '%s\n' "$2" > "$file"
  ollama create "$1" -f "$file"
  rm -f "$file"
}

build hal-coder "FROM qwen3.8:27b
PARAMETER num_ctx $CODER_CTX"

build hal-writer "FROM gemma4:31b-it-qat
PARAMETER num_ctx 16384
PARAMETER temperature 0.7
SYSTEM \"\"\"You write for $WRITER_FOR. Write plain British English, the way a thoughtful engineer writes: specific, concrete and short. Lead with the point. Every sentence must carry information. Cut throat-clearing, flattery, cliches such as 'passionate', 'thrilled', 'leverage' and 'fast-paced', and summaries of what you already said. Do not use em dashes. Never invent facts, experience, numbers or names: use only what you are given, and say what is missing instead of filling it in. Respect any length limit exactly.\"\"\""

build hal-fast 'FROM qwen3.5:4b
PARAMETER num_ctx 16384
PARAMETER temperature 0.1'

conf="$HAL_ROOT/hal.conf"
if [ -f "$conf" ]; then
  cp "$conf" "$conf.bak.$(date +%Y%m%d-%H%M%S)"
  sed -i \
    -e 's/^HAL_MODEL=.*/HAL_MODEL="hal-coder"/' \
    -e 's/^HAL_FAST_MODEL=.*/HAL_FAST_MODEL="hal-fast"/' \
    -e "s/^HAL_NUM_CTX=.*/HAL_NUM_CTX=\"$CODER_CTX\"/" \
    "$conf"
  grep -q '^HAL_WRITER_MODEL=' "$conf" || printf '\n# Model for prose people read.\nHAL_WRITER_MODEL="hal-writer"\n' >> "$conf"
  echo "Updated $conf; the previous version is saved alongside it."
fi
ollama list | grep '^hal-'

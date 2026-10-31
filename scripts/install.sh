#!/usr/bin/env bash
# Installs Hal from this checkout into a private uv venv and links `hal` into ~/.local/bin.
#
# Never uses sudo and never pipes a download into a shell (REQ-SYS-17, REQ-CLI-89). It is
# idempotent: a second run reinstalls the same editable packages and leaves the link as it is.
# `--dry-run` prints every step and performs none. Host setup that needs root (gVisor) stays
# a separate, explicit step: see docs/howto/install-gvisor.md.
set -euo pipefail

DRY_RUN=0
CORE_ONLY=0
NO_LINK=0
usage() {
  cat <<'USAGE'
usage: scripts/install.sh [--dry-run] [--core-only] [--no-link]
  --dry-run    print what would be done, change nothing
  --core-only  install only kit, core, hub, sandbox and agent (no gateway, no remote)
  --no-link    do not create ~/.local/bin/hal
USAGE
}
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --core-only) CORE_ONLY=1 ;;
    --no-link) NO_LINK=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "install.sh: unknown option $arg" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/hal"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
PACKAGES=(hal-kit hal-core hal-hub hal-sandbox hal-agent)
if [ "$CORE_ONLY" -eq 0 ]; then PACKAGES+=(hal-gateway hal-remote); fi

step() { # print the step; run it unless this is a dry run
  echo "+ $*"
  if [ "$DRY_RUN" -eq 0 ]; then "$@"; fi
}

if ! command -v uv > /dev/null 2>&1; then
  echo "install.sh: uv is not installed; get it from https://docs.astral.sh/uv/ and rerun" >&2
  exit 3
fi
echo "Hal install from $ROOT into $VENV"
if [ ! -x "$VENV/bin/python" ]; then
  step mkdir -p "$DATA"
  step uv venv --quiet "$VENV"
else
  echo "= venv exists, kept"
fi
args=()
for pkg in "${PACKAGES[@]}"; do args+=(-e "$ROOT/packages/$pkg"); done
step uv pip install --quiet --python "$VENV/bin/python" "${args[@]}"
if [ "$NO_LINK" -eq 0 ]; then
  if [ "$(readlink "$BIN/hal" 2> /dev/null || true)" = "$VENV/bin/hal" ]; then
    echo "= $BIN/hal already points at the venv"
  elif [ -e "$BIN/hal" ] && [ ! -L "$BIN/hal" ]; then
    echo "install.sh: $BIN/hal is a regular file; move it away, then rerun" >&2
    exit 1
  else
    step mkdir -p "$BIN"
    step ln -sfn "$VENV/bin/hal" "$BIN/hal"
  fi
fi
echo "done; next: hal doctor"

#!/usr/bin/env bash
# Pulls every model in catalog.txt, in order, skipping ones already present.
# Safe to stop and rerun: Ollama resumes partial downloads.
#
#   ./models/pull-all.sh            # all models
#   ./models/pull-all.sh --roles    # only the role models (first block)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${HAL_LOG_DIR:-$HOME/.local/state/hal}/pull-all.log"
mkdir -p "$(dirname "$LOG")"

tags=$(awk '
  /^# Full-GPU/ { if (only_roles) exit }
  /^[a-z0-9]/ { print $1 }
' only_roles="$([ "${1:-}" = "--roles" ] && echo 1)" "$HERE/catalog.txt")

have=$(ollama list | awk 'NR > 1 { print $1 }')
failed=()
for tag in $tags; do
  name="$tag"; [[ "$name" == *:* ]] || name="$name:latest"
  if grep -qx "$name" <<< "$have"; then
    echo "$(date -Is) skip $tag" >> "$LOG"
    continue
  fi
  echo "$(date -Is) pull $tag" >> "$LOG"
  for attempt in 1 2 3; do
    # Progress bars redraw thousands of times; keep only the final lines.
    if out=$(ollama pull "$tag" 2>&1); then
      tail -n 2 <<< "$out" | tr -d '\r' >> "$LOG"
      echo "$(date -Is) done $tag" >> "$LOG"
      break
    fi
    tail -n 3 <<< "$out" | tr -d '\r' >> "$LOG"
    [ "$attempt" -eq 3 ] && failed+=("$tag")
    sleep $((attempt * 30))
  done
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "$(date -Is) failed: ${failed[*]}" >> "$LOG"
  exit 1
fi
echo "$(date -Is) all models present" >> "$LOG"

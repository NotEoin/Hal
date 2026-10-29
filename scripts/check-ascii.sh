#!/usr/bin/env bash
# ASCII gate (REQ-SYS-20): fails when a tracked text file or a file name holds a byte above 0x7F.
#
# Usage: scripts/check-ascii.sh
# Checks every file git tracks or would track (not ignored). Binary files are skipped, since a
# binary is not text we wrote. Files listed in scripts/ascii-exempt.txt are skipped; each line
# there is `path  # reason`. Exit status: 0 clean, 1 findings, 2 not run in a git checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXEMPT_FILE="scripts/ascii-exempt.txt"
cd "$ROOT"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "check-ascii: not a git checkout" >&2; exit 2; }

declare -A exempt=()
if [ -f "$EXEMPT_FILE" ]; then
  while IFS= read -r line; do
    path="${line%%#*}"
    path="${path%"${path##*[![:space:]]}"}"   # trim trailing blanks
    [ -n "$path" ] && exempt["$path"]=1
  done < "$EXEMPT_FILE"
fi

status=0
while IFS= read -r -d '' file; do
  [ -f "$file" ] || continue                  # listed but deleted in the work tree
  if printf '%s' "$file" | LC_ALL=C grep -qP '[^\x00-\x7F]'; then
    echo "check-ascii: non-ASCII file name: $file" >&2
    status=1
    continue
  fi
  [ -n "${exempt[$file]:-}" ] && continue
  # -I treats binary files as non-matching; -n prints the line number
  if hits="$(LC_ALL=C grep -InP '[^\x00-\x7F]' -- "$file")"; then
    while IFS= read -r hit; do echo "check-ascii: $file:$hit" >&2; done <<< "$hits"
    status=1
  fi
done < <(git ls-files -z --cached --others --exclude-standard)

if [ "$status" -eq 0 ]; then echo "check-ascii: ok"; fi
exit "$status"

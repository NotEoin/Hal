#!/usr/bin/env bash
#
# import-vault.sh — Phase 1 of importing an old Obsidian vault into Hal.
#
# Does the SAFE, mechanical part only: makes a lossless COPY of your old vault into a
# staging folder inside your Hal vault, and writes an inventory (IMPORT-MANIFEST.md) that
# the /import-vault skill reads to reclassify notes intelligently.
#
# Your original vault is never modified. Nothing is reorganised here — that happens later,
# under your control, via:   hal   →   /import-vault
#
# Usage:
#   ./import-vault.sh /path/to/OldVault            # copy + inventory
#   ./import-vault.sh /path/to/OldVault --dry-run  # inventory only, no copy
#
set -uo pipefail

HAL_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VAULT="${HAL_VAULT:-$HAL_ROOT/Vault}"

c_blue=$'\033[1;36m'; c_yellow=$'\033[1;33m'; c_green=$'\033[1;32m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '\n%s==> %s%s\n' "$c_blue" "$*" "$c_off"; }
ok()   { printf '%s  ✓ %s%s\n' "$c_green" "$*" "$c_off"; }
warn() { printf '%s  ! %s%s\n' "$c_yellow" "$*" "$c_off"; }
err()  { printf '%s  ✗ %s%s\n' "$c_red" "$*" "$c_off"; }

# ---- Args -------------------------------------------------------------------
SRC="${1:-}"
DRY_RUN=0
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1
[ "${1:-}" = "--dry-run" ] && { err "Pass the old vault path first: ./import-vault.sh /path/to/OldVault --dry-run"; exit 1; }

if [ -z "$SRC" ]; then
  err "Usage: ./import-vault.sh /path/to/OldVault [--dry-run]"; exit 1
fi
if [ ! -d "$SRC" ]; then
  err "Not a directory: $SRC"; exit 1
fi
SRC="$(cd "$SRC" && pwd)"                 # absolute
NAME="$(basename "$SRC")"
STAGING="$VAULT/_Import-Staging/$NAME"

# Sanity: does the source look like a vault (any .md files)?
MD_COUNT_SRC="$(find "$SRC" -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
if [ "$MD_COUNT_SRC" -eq 0 ]; then
  warn "No .md files found under $SRC — is this the right folder? Continuing anyway."
fi

printf '%s\n' "============================================================"
printf '   Import old vault → Hal (Phase 1: copy + inventory)\n'
printf '   From:    %s\n' "$SRC"
printf '   Into:    %s\n' "$STAGING"
printf '   Mode:    %s\n' "$([ $DRY_RUN -eq 1 ] && echo 'DRY RUN (no copy)' || echo 'copy')"
printf '%s\n' "============================================================"

EXCLUDES=( ".obsidian" ".obsidian-git" ".trash" ".git" ".DS_Store" )

# ---- Copy (unless dry run) --------------------------------------------------
if [ "$DRY_RUN" -eq 0 ]; then
  log "Copying (originals are left untouched)…"
  mkdir -p "$STAGING"
  if command -v rsync >/dev/null 2>&1; then
    RS_EXCL=(); for e in "${EXCLUDES[@]}"; do RS_EXCL+=( --exclude "$e" ); done
    rsync -a "${RS_EXCL[@]}" "$SRC/" "$STAGING/" && ok "Copied to staging" || { err "Copy failed"; exit 1; }
  else
    cp -a "$SRC/." "$STAGING/" && ok "Copied to staging (cp)" || { err "Copy failed"; exit 1; }
    for e in "${EXCLUDES[@]}"; do rm -rf "$STAGING/$e"; done
  fi
  SCAN="$STAGING"
else
  SCAN="$SRC"
fi

# ---- Inventory --------------------------------------------------------------
log "Building inventory…"
TOTAL_MD="$(find "$SCAN" -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
TOTAL_FILES="$(find "$SCAN" -type f 2>/dev/null | wc -l | tr -d ' ')"
TOTAL_DIRS="$(find "$SCAN" -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
ATTACH="$(find "$SCAN" -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.gif' -o -iname '*.pdf' -o -iname '*.webp' -o -iname '*.svg' -o -iname '*.mp4' -o -iname '*.mp3' -o -iname '*.xlsx' -o -iname '*.docx' -o -iname '*.pptx' \) 2>/dev/null | wc -l | tr -d ' ')"

# Per top-level folder note counts
TOPDIRS_REPORT="$(
  while IFS= read -r -d '' d; do
    n=$(find "$d" -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
    printf '| %s | %s |\n' "$(basename "$d")" "$n"
  done < <(find "$SCAN" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print0 2>/dev/null | sort -z)
)"
ROOT_MD="$(find "$SCAN" -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"

# File-type breakdown (top 12 extensions)
TYPES_REPORT="$(
  find "$SCAN" -type f 2>/dev/null | sed -n 's/.*\.\([A-Za-z0-9]\{1,5\}\)$/\1/p' \
    | tr 'A-Z' 'a-z' | sort | uniq -c | sort -rn | head -12 \
    | awk '{printf "| .%s | %s |\n", $2, $1}'
)"

# Duplicate filenames (the link-safety risk for reclassification)
DUPES="$(find "$SCAN" -type f -name '*.md' -printf '%f\n' 2>/dev/null | sort | uniq -d)"
DUPE_COUNT="$(printf '%s' "$DUPES" | grep -c . || true)"
DUPES_REPORT="$(
  if [ -n "$DUPES" ]; then
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      printf '\n- **%s** appears in:\n' "$f"
      find "$SCAN" -type f -name "$f" -printf '  - %p\n' 2>/dev/null | sed "s|$SCAN/||"
    done <<< "$DUPES"
  else
    printf '_None — every note filename is unique, so wikilinks will survive reclassification._\n'
  fi
)"

# ---- Write manifest ---------------------------------------------------------
MANIFEST_DIR="$([ $DRY_RUN -eq 1 ] && echo "$VAULT/_Import-Staging" || echo "$STAGING")"
mkdir -p "$MANIFEST_DIR"
MANIFEST="$MANIFEST_DIR/IMPORT-MANIFEST${DRY_RUN:+}.md"
[ "$DRY_RUN" -eq 1 ] && MANIFEST="$MANIFEST_DIR/IMPORT-MANIFEST-dryrun.md"

{
  echo "---"
  echo "type: note"
  echo "tags: [system, import]"
  echo "date: $(date +%F)"
  echo "---"
  echo
  echo "# Import Manifest — $NAME"
  echo
  echo "> Generated by \`import-vault.sh\` on $(date '+%Y-%m-%d %H:%M'). Source: \`$SRC\`"
  echo "> The \`/import-vault\` skill reads this to plan reclassification. Originals untouched."
  echo
  echo "## Totals"
  echo
  echo "| Metric | Count |"
  echo "|---|---|"
  echo "| Markdown notes | $TOTAL_MD |"
  echo "| Attachments (img/pdf/media/office) | $ATTACH |"
  echo "| All files | $TOTAL_FILES |"
  echo "| Folders | $TOTAL_DIRS |"
  echo "| Notes in vault root | $ROOT_MD |"
  echo "| Duplicate note filenames | $DUPE_COUNT |"
  echo
  echo "## Top-level folders (note counts)"
  echo
  echo "| Folder | Notes |"
  echo "|---|---|"
  [ -n "$TOPDIRS_REPORT" ] && echo "$TOPDIRS_REPORT"
  echo
  echo "## File types"
  echo
  echo "| Extension | Count |"
  echo "|---|---|"
  [ -n "$TYPES_REPORT" ] && echo "$TYPES_REPORT"
  echo
  echo "## ⚠️ Duplicate filenames (link-safety check)"
  echo
  echo "These names exist in more than one folder. Because Obsidian resolves \`[[links]]\` by"
  echo "filename, duplicates must be disambiguated during import or links may point to the wrong"
  echo "note. \`/import-vault\` will propose renames for these."
  echo
  echo "$DUPES_REPORT"
} > "$MANIFEST"

ok "Inventory written: ${MANIFEST/#$VAULT\//Vault/}"

# ---- Summary to terminal ----------------------------------------------------
cat <<EOF

${c_green}Phase 1 complete.${c_off}
  Notes:        $TOTAL_MD
  Attachments:  $ATTACH
  Folders:      $TOTAL_DIRS
  Dup names:    $DUPE_COUNT $( [ "$DUPE_COUNT" -gt 0 ] && printf '%s(will be disambiguated on import)%s' "$c_yellow" "$c_off" )

Next:
EOF
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  This was a dry run (nothing copied). Re-run without --dry-run to stage the copy:"
  echo "    ./import-vault.sh \"$SRC\""
else
  echo "  1) Launch Hal:   hal"
  echo "  2) Run:          /import-vault"
  echo "     Hal will read the manifest, propose where each folder goes, then reclassify"
  echo "     into Areas/Study (and other Areas) with frontmatter — folder by folder, with"
  echo "     your confirmation. Your staging copy stays until you say to remove it."
fi

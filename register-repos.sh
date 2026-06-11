#!/usr/bin/env bash
#
# register-repos.sh — add your coding projects to the second brain as a *linked layer*.
#
# It scans folders for git repos (or registers a single repo / a GitHub-only URL) and writes
# one lightweight "hub note" per repo into Vault/Areas/Code/. The code itself is NEVER copied
# and file *contents* are never read — only safe git metadata + a file-extension tally are
# used, and any credentials embedded in a remote URL are stripped. The smart part (reading
# READMEs, wiring per-repo CLAUDE.md memory) is the `/index-repos` skill — run that after.
#
# Usage:
#   ./register-repos.sh ~/code ~/projects        # scan these parent folders for git repos
#   ./register-repos.sh ~/code/myproject         # register one repo directly
#   ./register-repos.sh --github URL [Name]      # add a GitHub-only repo as a card
#   ./register-repos.sh --depth 4 ~/code         # how deep to look for .git (default 4)
#   ./register-repos.sh --refresh ~/code         # update git metadata on existing notes
#   ./register-repos.sh                          # scan common dev folders (~/code, ~/dev, …)
#
# Re-runnable: existing notes are skipped (use --refresh to update their metadata only;
# your edits and the /index-repos summary in the note body are preserved).
#
set -uo pipefail

HAL_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VAULT="${HAL_VAULT:-$HAL_ROOT/Vault}"
CODE="$VAULT/Areas/Code"
REPORT="$CODE/CODE-REPORT.md"

c_blue=$'\033[1;36m'; c_yellow=$'\033[1;33m'; c_green=$'\033[1;32m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '\n%s==> %s%s\n' "$c_blue" "$*" "$c_off"; }
ok()   { printf '%s  ✓ %s%s\n' "$c_green" "$*" "$c_off"; }
warn() { printf '%s  ! %s%s\n' "$c_yellow" "$*" "$c_off"; }
err()  { printf '%s  ✗ %s%s\n' "$c_red" "$*" "$c_off"; }
have() { command -v "$1" >/dev/null 2>&1; }

if ! have git; then err "git is not installed — needed to read repo metadata."; exit 1; fi
mkdir -p "$CODE"

# ---- Args -------------------------------------------------------------------
DEPTH=4; REFRESH=0; GH_URL=""; GH_NAME=""; SCAN_PATHS=()
args=( "$@" )
i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    --depth)   i=$((i+1)); DEPTH="${args[$i]:-4}" ;;
    --refresh) REFRESH=1 ;;
    --github)  i=$((i+1)); GH_URL="${args[$i]:-}"; n="${args[$((i+1))]:-}"; case "$n" in ""|--*) ;; *) GH_NAME="$n"; i=$((i+1));; esac ;;
    --help|-h) sed -n '3,21p' "$0"; exit 0 ;;
    *) SCAN_PATHS+=( "$a" ) ;;
  esac
  i=$((i+1))
done

# Default scan locations if none supplied (only those that exist)
if [ ${#SCAN_PATHS[@]} -eq 0 ] && [ -z "$GH_URL" ]; then
  for d in "$HOME/code" "$HOME/Code" "$HOME/dev" "$HOME/Dev" "$HOME/src" "$HOME/projects" "$HOME/Projects" "$HOME/repos" "$HOME/git" "$HOME/Documents/coding"; do
    [ -d "$d" ] && SCAN_PATHS+=( "$d" )
  done
  [ ${#SCAN_PATHS[@]} -eq 0 ] && { err "No paths given and no common dev folder found. Try: ./register-repos.sh ~/your/code"; exit 1; }
  warn "No paths given — scanning defaults: ${SCAN_PATHS[*]}"
fi

# ---- Helpers ----------------------------------------------------------------
slug() { printf '%s' "$1" | tr 'A-Z' 'a-z' | tr -cs 'a-z0-9' '-' | sed 's/^-//; s/-$//'; }

strip_creds() { # remove user:token@ from a remote URL
  printf '%s' "$1" | sed -E 's#(https?://)[^/@]*@#\1#'
}

ext2lang() {
  case "$1" in
    py) echo Python;; ipynb) echo "Jupyter Notebook";;
    js|mjs|cjs) echo JavaScript;; ts) echo TypeScript;; tsx|jsx) echo "TypeScript/React";;
    go) echo Go;; rs) echo Rust;; java) echo Java;; kt|kts) echo Kotlin;;
    c|h) echo C;; cpp|cc|cxx|hpp|hh) echo C++;; cs) echo "C#";;
    rb) echo Ruby;; php) echo PHP;; swift) echo Swift;; m|mm) echo Objective-C;;
    sh|bash|zsh) echo Shell;; lua) echo Lua;; r) echo R;; jl) echo Julia;;
    dart) echo Dart;; vue) echo Vue;; svelte) echo Svelte;; sql) echo SQL;;
    scala) echo Scala;; ex|exs) echo Elixir;; erl) echo Erlang;; hs) echo Haskell;;
    clj|cljs) echo Clojure;; pl) echo Perl;; ml) echo OCaml;; nim) echo Nim;; zig) echo Zig;;
    *) echo "";;
  esac
}

code_files() { # list candidate code files, pruning noisy dirs
  find "$1" \
    \( -type d \( -name .git -o -name node_modules -o -name vendor -o -name dist -o -name build \
       -o -name .venv -o -name venv -o -name env -o -name target -o -name .next -o -name .nuxt \
       -o -name .gradle -o -name __pycache__ -o -name .cache -o -name .terraform -o -name Pods \
       -o -name .dart_tool -o -name coverage -o -name out \) -prune \) -o \
    -type f -print 2>/dev/null
}

detect_language() {
  local repo="$1" topext lang
  topext="$(code_files "$repo" \
    | sed -n 's/.*\.\([A-Za-z0-9]\{1,7\}\)$/\1/p' \
    | tr 'A-Z' 'a-z' \
    | grep -Ev '^(md|markdown|txt|rst|json|ya?ml|toml|ini|cfg|conf|lock|csv|tsv|xml|svg|png|jpe?g|gif|webp|ico|pdf|log|map|min|gitignore|gitattributes|editorconfig|env|example|sample|d|ts)$' \
    | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')"
  lang="$(ext2lang "${topext:-}")"
  if [ -z "$lang" ]; then
    # fall back to .ts (filtered above to avoid .d.ts noise) then to manifest files
    topext="$(code_files "$repo" | sed -n 's/.*\.\(ts\)$/\1/p' | head -1)"
    [ "$topext" = "ts" ] && lang="TypeScript"
  fi
  if [ -z "$lang" ]; then
    [ -f "$repo/package.json" ]      && lang="JavaScript/TypeScript"
    [ -f "$repo/pyproject.toml" ] || [ -f "$repo/requirements.txt" ] && lang="Python"
    [ -f "$repo/Cargo.toml" ]        && lang="Rust"
    [ -f "$repo/go.mod" ]            && lang="Go"
    [ -f "$repo/pom.xml" ] || [ -f "$repo/build.gradle" ] && lang="Java"
    [ -f "$repo/Gemfile" ]           && lang="Ruby"
  fi
  printf '%s' "${lang:-Unknown}"
}

detect_stack() { # echo a comma list of build/manifest files present at repo root
  local repo="$1" hits=()
  for f in package.json pyproject.toml requirements.txt Cargo.toml go.mod pom.xml \
           build.gradle Gemfile composer.json Dockerfile docker-compose.yml \
           Makefile tsconfig.json .nvmrc flake.nix; do
    [ -e "$repo/$f" ] && hits+=( "$f" )
  done
  ( IFS=', '; printf '%s' "${hits[*]}" )
}

find_readme() {
  local repo="$1" f
  for f in README.md README.MD Readme.md README README.txt README.rst readme.md; do
    [ -f "$repo/$f" ] && { printf '%s' "$f"; return; }
  done
  printf ''
}

# Find an existing hub note whose frontmatter `path:` or `repo:` matches; echo its file
existing_note_for() { # $1=key (path|repo)  $2=value
  grep -rlF --include='*.md' "$1: $2" "$CODE" 2>/dev/null | head -n1
}

# Rebuild only the volatile frontmatter fields of an existing note, preserve everything else
refresh_note() { # $1=file  rest=key=val pairs
  local file="$1"; shift
  local tmp; tmp="$(mktemp)"
  awk -v updates="$*" '
    BEGIN{ n=split(updates, kv, " ");
           for(i=1;i<=n;i++){ split(kv[i], p, "="); U[p[1]]=substr(kv[i], index(kv[i],"=")+1) } }
    NR==1 && $0=="---" { infm=1; print; next }
    infm && $0=="---" { infm=0; print; next }
    infm {
      key=$0; sub(/:.*/,"",key);
      if (key in U) { print key ": " U[key]; delete U[key]; next }
    }
    { print }
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

# ---- Write a hub note for one local repo ------------------------------------
write_local_note() { # $1=repo abs path
  local repo="$1" name slugn lang stack readme remote branch last_commit loc note today
  name="$(basename "$repo")"
  lang="$(detect_language "$repo")"
  stack="$(detect_stack "$repo")"
  readme="$(find_readme "$repo")"
  remote="$(git -C "$repo" remote get-url origin 2>/dev/null || git -C "$repo" config --get remote.origin.url 2>/dev/null || true)"
  remote="$(strip_creds "${remote:-}")"
  branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  last_commit="$(git -C "$repo" log -1 --format=%cs 2>/dev/null || echo '')"
  today="$(date +%F)"
  if [ -n "$remote" ]; then loc="local+github"; else loc="local"; fi

  # Idempotency / refresh
  local hit; hit="$(existing_note_for path "$repo")"
  if [ -n "$hit" ]; then
    if [ "$REFRESH" -eq 1 ]; then
      refresh_note "$hit" "language=$lang" "location=$loc" "branch=$branch" "last_commit=${last_commit:-?}" "repo=${remote:-}"
      ok "refreshed: ${hit#$VAULT/}"; R_REFRESH=$((R_REFRESH+1))
    else
      R_SKIP=$((R_SKIP+1))
    fi
    REG_ROWS+=( "$name|$lang|$loc|$([ -n "$readme" ] && echo yes || echo no)|$([ -f "$repo/CLAUDE.md" ] && echo yes || echo no)|${last_commit:-?}" )
    return
  fi

  # Collision-safe filename
  note="$CODE/$name.md"
  [ -e "$note" ] && note="$CODE/$name-$(basename "$(dirname "$repo")").md"
  [ -e "$note" ] && note="$CODE/$name-$(printf '%s' "$repo" | cksum | cut -d' ' -f1).md"
  slugn="$(slug "$lang")"

  {
    echo "---"
    echo "type: code-project"
    echo "date: ${last_commit:-$today}"
    echo "project: $name"
    echo "language: $lang"
    echo "location: $loc"
    echo "path: $repo"
    echo "repo: ${remote:-}"
    echo "branch: $branch"
    echo "last_commit: ${last_commit:-?}"
    echo "status: active"
    echo "tags: [code${slugn:+, $slugn}]"
    echo "---"
    echo
    echo "# $name"
    echo
    echo "**Language:** $lang   ·   **Where:** \`$repo\`${remote:+   ·   [$remote]($remote)}"
    echo "**Branch:** \`$branch\`   ·   **Last commit:** ${last_commit:-unknown}"
    echo
    echo "## What it is"
    echo "<!-- /index-repos fills this from the README. -->"
    [ -z "$readme" ] && echo "_No README found in the repo._"
    echo
    echo "## Stack / Key files"
    if [ -n "$stack" ]; then echo "Detected: \`$stack\`"; else echo "_Not detected automatically._"; fi
    echo
    echo "## How to run"
    echo '```bash'
    echo "# cd into the repo and open it with Hal's per-repo memory:"
    echo "hal here   # run this inside $repo"
    echo '```'
    echo
    echo "## Notes"
    echo
    echo "## TODO"
    echo "- [ ] "
    echo
    echo "## Deep memory"
    echo "> Open the repo (\`hal here\`) and run \`/init\` once to generate a code-aware"
    echo "> \`CLAUDE.md\` inside it. \`/index-repos\` can do this for you."
    if [ -f "$repo/CLAUDE.md" ]; then echo ">"; echo "> ✅ This repo already has a \`CLAUDE.md\`."; fi
    echo
    echo "## Recent Sessions"
    echo '```dataview'
    echo "TABLE date AS Date, file.link AS Log"
    echo 'FROM "Areas/Code/_Session-Logs"'
    echo 'WHERE type = "session" AND project = this.project'
    echo "SORT date DESC"
    echo '```'
  } > "$note"

  ok "registered: ${note#$VAULT/}  ($lang)"
  R_NEW=$((R_NEW+1))
  REG_ROWS+=( "$name|$lang|$loc|$([ -n "$readme" ] && echo yes || echo no)|$([ -f "$repo/CLAUDE.md" ] && echo yes || echo no)|${last_commit:-?}" )
}

# ---- Write a hub note for a GitHub-only repo --------------------------------
write_github_note() { # $1=url  $2=name(optional)
  local url name note today
  url="$(strip_creds "$1")"
  name="${2:-}"
  [ -z "$name" ] && { name="$(basename "$url")"; name="${name%.git}"; }
  today="$(date +%F)"

  local hit; hit="$(existing_note_for repo "$url")"
  if [ -n "$hit" ] && [ "$REFRESH" -eq 0 ]; then R_SKIP=$((R_SKIP+1)); REG_ROWS+=( "$name|—|github|?|?|—" ); return; fi

  note="$CODE/$name.md"
  [ -e "$note" ] && note="$CODE/$name-github.md"
  {
    echo "---"
    echo "type: code-project"
    echo "date: $today"
    echo "project: $name"
    echo "language: "
    echo "location: github"
    echo "path: "
    echo "repo: $url"
    echo "branch: "
    echo "last_commit: "
    echo "status: active"
    echo "tags: [code]"
    echo "---"
    echo
    echo "# $name"
    echo
    echo "**Remote:** [$url]($url)   ·   _Not cloned locally._"
    echo
    echo "## What it is"
    echo "<!-- Fill in, or clone it locally then re-run register-repos.sh to auto-detect. -->"
    echo
    echo "## TODO"
    echo "- [ ] Clone locally if you want Hal to work on it: \`git clone $url\`"
  } > "$note"
  ok "registered (GitHub card): ${note#$VAULT/}"
  R_NEW=$((R_NEW+1))
  REG_ROWS+=( "$name|—|github|?|?|—" )
}

# ---- Run --------------------------------------------------------------------
R_NEW=0; R_SKIP=0; R_REFRESH=0; REG_ROWS=()

if [ -n "$GH_URL" ]; then
  log "Adding GitHub-only repo card"
  write_github_note "$GH_URL" "$GH_NAME"
fi

declare -A SEEN
for base in "${SCAN_PATHS[@]:-}"; do
  [ -z "$base" ] && continue
  if [ ! -d "$base" ]; then warn "Not a directory, skipping: $base"; continue; fi
  base="$(cd "$base" && pwd)"

  # Is the path itself a repo?
  if [ -d "$base/.git" ]; then
    [ -n "${SEEN[$base]:-}" ] || { SEEN[$base]=1; log "Repo: ${base/#$HOME/~}"; write_local_note "$base"; }
    continue
  fi

  log "Scanning for git repos under: ${base/#$HOME/~}  (depth ≤ $DEPTH)"
  while IFS= read -r -d '' gitdir; do
    repo="$(dirname "$gitdir")"
    [ -n "${SEEN[$repo]:-}" ] && continue
    SEEN[$repo]=1
    write_local_note "$repo"
  done < <(find "$base" -maxdepth "$DEPTH" -type d -name .git -prune -print0 2>/dev/null | sort -z)
done

# ---- Report -----------------------------------------------------------------
{
  echo "---"
  echo "type: note"
  echo "tags: [system, code, index]"
  echo "date: $(date +%F)"
  echo "---"
  echo
  echo "# Code Repos Report"
  echo
  echo "> Generated by \`register-repos.sh\` on $(date '+%Y-%m-%d %H:%M')."
  echo "> Hub notes live in \`Areas/Code/\`; the dashboard is \`Areas/Code/_Index.md\`."
  echo "> Run \`hal\` → \`/index-repos\` to fill summaries from READMEs and wire up per-repo memory."
  echo
  echo "| Repo | Language | Where | README | CLAUDE.md | Last commit |"
  echo "|---|---|---|---|---|---|"
  for row in "${REG_ROWS[@]:-}"; do
    [ -z "$row" ] && continue
    IFS='|' read -r a b c d e f <<< "$row"
    printf '| %s | %s | %s | %s | %s | %s |\n' "$a" "$b" "$c" "$d" "$e" "$f"
  done
  echo
  echo "## Summary"
  echo
  echo "- Newly registered: **$R_NEW**"
  echo "- Already present (skipped): **$R_SKIP**"
  [ "$R_REFRESH" -gt 0 ] && echo "- Metadata refreshed: **$R_REFRESH**"
  echo
  echo "Next: \`hal\` → \`/index-repos\` (summaries + \`/init\` per-repo memory + session-log mirroring)."
} > "$REPORT"

cat <<EOF

${c_green}Repo registration complete.${c_off}
  Newly registered: $R_NEW
  Skipped (exist):  $R_SKIP
  Refreshed:        $R_REFRESH

Hub notes: Vault/Areas/Code/   ·   Report: Areas/Code/CODE-REPORT.md
Next:      hal  →  /index-repos
EOF

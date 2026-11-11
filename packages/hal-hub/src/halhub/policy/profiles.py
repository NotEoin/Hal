"""Built-in profiles, channel profiles and the hard-deny lists of the tool policy.

01-security, "`policy.toml` schema", fixes every value here. The built-in profiles are
`default`, `strict`, `trusted` and `standalone` (RESOLUTIONS C9: `default` minus the `system`
tier, for a pack's own CLI and MCP server when Hal is not installed). The shipped per-tool
entries are the only place a shipped tool gets a verdict other than its tier's, and each
carries its reason (residual risk 9 lists the zero-prompt sends). An entry for a tool that is
not installed is simply never consulted.

The hard-deny paths and file patterns cannot be removed by `policy.toml` (REQ-SEC-08):
the loader only adds `[paths].deny` entries to them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .model import ChannelProfile, Profile, Rule, Scope, Tier, Verdict

PROFILE_NAMES = ("default", "strict", "trusted", "standalone")
CHANNEL_NAMES = ("voice", "telegram", "remote")
BOTH_SCOPES = (Scope.ONCE, Scope.SESSION)
PROMPT_SCOPES = frozenset(BOTH_SCOPES)  # what a prompt may ever offer; ROUTINE never (B2)

DEFAULT_TIERS: Mapping[Tier, Verdict] = {
    Tier.READ: Verdict.ALLOW,
    Tier.WRITE: Verdict.ALLOW,
    Tier.ACT: Verdict.ASK,
    Tier.SEND: Verdict.ASK,
    Tier.SYSTEM: Verdict.ASK,
}

# send tools with destinations fixed in the user's config, not chosen by the model (C4, SD19)
FIXED_DESTINATION_SENDS: Mapping[str, str] = {
    "notify_send": "push to the user's own configured topics and devices",
    "briefing_deliver": "delivers the briefing to the user's configured channel",
    "calendar_sync": "syncs with the CalDAV account in config; the secret stays host side",
}
# tightenings carried from 08's per-tool table
SHIPPED_TIGHTENINGS: Mapping[str, str] = {
    "calendar_delete": "destructive write",
    "tasks_delete": "destructive write",
    "habits_remove": "destructive write",
    "desktop_clipboard": "read tier, but the clipboard often holds passwords",
}

_DEFAULT_TOOLS: Mapping[str, Rule] = {
    **{name: Rule(verdict=Verdict.ALLOW) for name in FIXED_DESTINATION_SENDS},
    **{name: Rule(verdict=Verdict.ASK) for name in SHIPPED_TIGHTENINGS},
}
_STRICT_TOOLS: Mapping[str, Rule] = {
    **_DEFAULT_TOOLS,
    **{name: Rule(verdict=Verdict.ASK) for name in FIXED_DESTINATION_SENDS},
}

PROFILES: Mapping[str, Profile] = {
    "default": Profile("default", DEFAULT_TIERS, Verdict.ALLOW, True, BOTH_SCOPES, _DEFAULT_TOOLS),
    "strict": Profile(
        "strict",
        {**DEFAULT_TIERS, Tier.WRITE: Verdict.ASK, Tier.SYSTEM: Verdict.DENY},
        Verdict.ASK,
        False,
        (Scope.ONCE,),
        _STRICT_TOOLS,
        egress_unlisted="deny",
        guard_on_flag="deny",
        sandbox_require="container",
    ),
    "trusted": Profile(
        "trusted",
        {**DEFAULT_TIERS, Tier.ACT: Verdict.ALLOW},
        Verdict.ALLOW,
        True,
        BOTH_SCOPES,
        _DEFAULT_TOOLS,
    ),
    "standalone": Profile(
        "standalone",
        {**DEFAULT_TIERS, Tier.SYSTEM: Verdict.DENY},
        Verdict.ALLOW,
        True,
        BOTH_SCOPES,
        _DEFAULT_TOOLS,
    ),
}

VOICE_APPROVAL_TIMEOUT_S = 20  # 01 "Channel profiles": a spoken answer comes now or never
TELEGRAM_APPROVAL_TIMEOUT_S = 600  # a phone answer may take minutes

CHANNEL_PROFILES: Mapping[str, ChannelProfile] = {
    "voice": ChannelProfile(
        "voice", frozenset({Tier.SYSTEM}), frozenset(), (Scope.ONCE,), VOICE_APPROVAL_TIMEOUT_S
    ),
    "telegram": ChannelProfile(
        "telegram", frozenset({Tier.SYSTEM}), frozenset(), (Scope.ONCE,), TELEGRAM_APPROVAL_TIMEOUT_S
    ),
    # the laptop harness is unsandboxed and its elicitation UI is not Hal's (07-remote)
    "remote": ChannelProfile("remote", frozenset({Tier.SYSTEM}), frozenset({"desktop"}), (Scope.ONCE,), None),
}

# 01 "Built-in hard-deny paths": credentials, browser profiles, Hal's own dirs and kernel trees.
HARD_DENY_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.azure",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.pki",
    "~/.password-store",
    "~/.local/share/keyrings",
    "~/.local/share/kwalletd",
    "~/.kde/share/apps/kwallet",
    "~/.config/gcloud",
    "~/.config/doctl",
    "~/.config/rclone",
    "~/.config/helm",
    "~/.config/op",
    "~/.config/sops",
    "~/.config/age",
    "~/.config/Bitwarden",
    "~/.config/1Password",
    "~/.config/KeePassXC",
    "~/.local/share/keepassxc",
    "~/.oci",
    "~/.terraform.d",
    "~/.vault-token",
    "~/.git-credentials",
    "~/.config/git/credentials",
    "~/.npmrc",
    "~/.pypirc",
    "~/.cargo/credentials.toml",
    "~/.cargo/credentials",
    "~/.bash_history",
    "~/.zsh_history",
    "~/.python_history",
    "~/.psql_history",
    "~/.mysql_history",
    "~/.node_repl_history",
    "~/.local/share/fish/fish_history",
    "~/.local/share/zsh/history",
    "~/.config/vivaldi",
    "~/.config/microsoft-edge",
    "~/.config/opera",
    "~/.config/BraveSoftware",
    "~/.librewolf",
    "~/.config/hal-employment",
    "~/snap/firefox",
    "~/snap/chromium",
    "~/.var/app/org.mozilla.firefox",
    "~/.var/app/org.chromium.Chromium",
    "~/.var/app/com.google.Chrome",
    "~/.var/app/com.brave.Browser",
    "~/.mozilla",
    "~/.config/google-chrome",
    "~/.config/chromium",
    "~/.config/gh",
    "~/.config/hal",
    "~/.local/state/hal",
    "~/.local/share/hal",
    "$XDG_RUNTIME_DIR",
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/run",
)
HARD_DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials*.json",
    "*.kdbx",
)


def expand(entry: str, *, home: Path, environ: Mapping[str, str] | None = None) -> Path | None:
    """`entry` with a leading `~/` or `$XDG_RUNTIME_DIR` expanded; None when the variable is unset."""
    if entry == "~" or entry.startswith("~/"):
        return Path(str(home) + entry[1:])
    if entry == "$XDG_RUNTIME_DIR":
        env = os.environ if environ is None else environ
        value = env.get("XDG_RUNTIME_DIR", "")
        return Path(value) if value else None
    return Path(entry)


def hard_deny_paths(*, home: Path, environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """The built-in hard-deny paths for `home`, expanded lexically (no file is touched)."""
    paths = (expand(entry, home=home, environ=environ) for entry in HARD_DENY_PATHS)
    return tuple(p for p in paths if p is not None)


# `hal policy init` writes this; every setting is commented out at its default, so the file
# changes nothing until the user edits it. The test uncomments every line and loads the result.
TEMPLATE = """\
# Hal tool policy. Reference: docs/design/01-security.md, "policy.toml schema".
# Every setting below is commented out at its default. Uncomment a line to change it.
schema_version = 1
# profile = "default"                # default | strict | trusted | standalone | a [profiles.<name>]

# [approval]
# timeout_s = 120                    # 10..3600: a prompt nobody answers becomes deny
# once_ttl_s = 600                   # 10..3600: an unused once grant is dropped after this
# session_max_h = 12                 # 1..72: a session grant ends with the session or after this
# routine_max_days = 90              # 1..365: a routine grant expires this long after approval
# notify = true                      # desktop notification when no terminal UI is attached

# [audit]
# content = false                    # log message bodies and result text in full
# args = true                        # log (redacted) arguments; false keeps only the key
# retention_days = 90                # 1..3650
# max_file_mb = 64                   # 1..1024: rotate a day file above this size

# [guard]
# enabled = false
# model = "prompt-guard-2-86m"       # or "prompt-guard-2-22m"
# threshold = 0.5                    # 0..1: malicious-class probability that flags
# max_scan_chars = 24000             # larger texts are flagged without scanning
# timeout_s = 15                     # per result
# threads = 2                        # 1..16 CPU threads for the worker
# on_flag = "ask"                    # ask | deny
# backend = "auto"                   # auto | docker | bwrap

# [reviewer]
# enabled = false
# model = "hal-classifier"           # gateway role alias
# timeout_s = 20
# when_untrusted = true              # may review while the session is untrusted

# [paths]
# read_roots = ["~/Documents", "~/Downloads", "~/Desktop", "~/Pictures", "~/Music", "~/Videos"]
# write_roots = ["~/Downloads"]
# deny = []                          # paths or globs added to the built-in hard-deny list

# [egress]
# domains = []                       # hosts allowed for every network tool
# unlisted = "ask"                   # ask | deny
# allow_http = false
# allow_ip_literals = false
# allow_private = false              # loopback, RFC 1918, link-local, *.local
# max_redirects = 3                  # 0..10
# search_tools = ["web_search"]      # tools whose result URLs feed url:from-search
# from_search = ["web_read"]         # tools whose URL arguments url:from-search may allow
# from_search_exclude = ["docs.google.com", "forms.gle"]  # hosts that accept writes by GET

# [rate]
# session = "2000/h"                 # all calls in one session
# asks = "40/h"                      # approval prompts in one session
# act = "120/h"
# send = "30/h"
# system = "10/h"

# [packs.organiser]                  # per pack, any profile
# enabled = true                     # false hides and denies every tool of the pack
# verdict = "ask"                    # allow | ask | deny: overrides the tier default
# reviewable = false
# domains = []

# [tools.web_read]                   # per tool, any profile
# enabled = true
# verdict = "ask"                    # system tools reject allow
# reviewable = false
# rate = "10/h"
# read_roots = []                    # replaces [paths].read_roots for this tool
# write_roots = []                   # replaces [paths].write_roots for this tool
# domains = ["docs.python.org"]      # hosts that turn ask into allow
# allow_args = {}                    # param -> list of exact values or globs; all must match

# [profiles.careful]                 # a custom profile, or overrides of a built-in one
# extends = "default"
# tiers = { read = "allow", write = "allow", act = "ask", send = "ask", system = "ask" }
# act_reversible = "ask"             # verdict for act tools with reversible = true
# reviewer = true                    # the reviewer may run under this profile
# scopes = ["once", "session"]       # subset of once and session; there is no always
"""

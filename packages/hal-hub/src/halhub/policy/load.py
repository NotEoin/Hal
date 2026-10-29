"""The `policy.toml` loader: schema checks, profile resolution and REQ-SEC-08's invariants.

`load_policy` reads `halkit.paths.policy_file()` (a missing file means the built-in `default`
profile) and returns a `Policy`. It fails loudly (REQ-SEC-14): a malformed file, an unknown
key, a value of the wrong type or outside its range, an unknown profile or channel, a cycle in
`extends`, or a rule that breaks an invariant raises `halkit.config.ConfigError` naming the
file, the dotted key and, when it can be found, the line. No front end runs with defaults
after an error.

Invariants the loader can check from the file alone: no profile's `system` tier is `allow`,
prompt scopes are a subset of `once` and `session`, and nothing removes a hard-deny path (no
key can). Invariants that need tool metadata (a `[tools.<t>]` entry allowing a `system` tool
or a `runs_code` tool, or making a `system` tool reviewable) are checked by
`check_rules(policy, metas)`, which the broker, `hal-hub mcp` and `hal policy check` run with
the registry's tools; `Policy.tool` clamps the same cases at run time as a second line.

The file must belong to the user and must not be writable by group or others (REQ-SEC-15),
because a box escape or another local user that could edit it would set every tool to
`allow`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import fields, replace
from pathlib import Path

from halkit import config as halconfig
from halkit import paths as halpaths

from .model import (
    ApprovalSettings,
    AuditSettings,
    EgressSettings,
    GuardSettings,
    PathSettings,
    Policy,
    Profile,
    Rate,
    RateSettings,
    ReviewerSettings,
    Rule,
    Scope,
    Tier,
    ToolMeta,
    Verdict,
)
from .profiles import CHANNEL_PROFILES, PROFILES, PROMPT_SCOPES

POLICY_SCHEMA_VERSION = 1
UNSAFE_MODE_BITS = 0o022  # group or other write (REQ-SEC-15)
ConfigError = halconfig.ConfigError

_TOP_KEYS = frozenset(
    {"schema_version", "profile", "approval", "audit", "guard", "reviewer", "paths", "egress", "rate"}
    | {"packs", "tools", "profiles"}
)

# (type, check) per key; a check returns an error message or None. Ranges are 01's schema comments.


def _range(low: float, high: float) -> Callable[[object], str | None]:
    return lambda v: None if low <= v <= high else f"must be between {low} and {high}"


def _choice(*options: str) -> Callable[[object], str | None]:
    return lambda v: None if v in options else f"must be one of {', '.join(options)}"


def _positive(v: object) -> str | None:
    return None if isinstance(v, int) and v > 0 else "must be a positive integer"


_Spec = Mapping[str, tuple[type | tuple[type, ...], Callable[[object], str | None] | None]]

_SECTIONS: Mapping[str, _Spec] = {
    "approval": {
        "timeout_s": (int, _range(10, 3600)),
        "once_ttl_s": (int, _range(10, 3600)),
        "session_max_h": (int, _range(1, 72)),
        "routine_max_days": (int, _range(1, 365)),
        "notify": (bool, None),
    },
    "audit": {
        "content": (bool, None),
        "args": (bool, None),
        "retention_days": (int, _range(1, 3650)),
        "max_file_mb": (int, _range(1, 1024)),
    },
    "guard": {
        "enabled": (bool, None),
        "model": (str, _choice("prompt-guard-2-86m", "prompt-guard-2-22m")),
        "threshold": ((int, float), _range(0, 1)),
        "max_scan_chars": (int, _positive),
        "timeout_s": (int, _positive),
        "threads": (int, _range(1, 16)),
        "on_flag": (str, _choice("ask", "deny")),
        "backend": (str, _choice("auto", "docker", "bwrap")),
    },
    "reviewer": {
        "enabled": (bool, None),
        "model": (str, None),
        "timeout_s": (int, _positive),
        "when_untrusted": (bool, None),
    },
    "paths": {"read_roots": (list, None), "write_roots": (list, None), "deny": (list, None)},
    "egress": {
        "domains": (list, None),
        "unlisted": (str, _choice("ask", "deny")),
        "allow_http": (bool, None),
        "allow_ip_literals": (bool, None),
        "allow_private": (bool, None),
        "max_redirects": (int, _range(0, 10)),
        "search_tools": (list, None),
        "from_search": (list, None),
        "from_search_exclude": (list, None),
    },
    "rate": {name: (str, None) for name in ("session", "asks", "act", "send", "system")},
}
_SETTINGS_TYPES = {
    "approval": ApprovalSettings,
    "audit": AuditSettings,
    "guard": GuardSettings,
    "reviewer": ReviewerSettings,
    "paths": PathSettings,
    "egress": EgressSettings,
    "rate": RateSettings,
}
_PACK_KEYS = frozenset({"enabled", "verdict", "reviewable", "domains"})
_TOOL_KEYS = _PACK_KEYS | {"rate", "read_roots", "write_roots", "allow_args"}
_PROFILE_KEYS = frozenset({"extends", "tiers", "act_reversible", "reviewer", "scopes", "packs", "tools"})
_DOMAIN_RE = re.compile(r"^(\*\.)?[a-z0-9_-]+(\.[a-z0-9_-]+)*$")


class _Loader:
    """One load: keeps the file path and text so every error can name key and line."""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.lines = text.split("\n")

    def fail(self, message: str, key: str | None = None) -> ConfigError:
        return ConfigError(
            f"{key + ': ' if key else ''}{message}",
            path=self.path,
            table="policy",
            key=key,
            line=self.line_of(key) if key else None,
        )

    def line_of(self, dotted: str) -> int | None:
        """Best-effort line of `dotted` (`a.b.c`): the key under its table header, else the header."""
        parts = dotted.split(".")
        for split in range(len(parts) - 1, -1, -1):
            table, key = ".".join(parts[:split]), ".".join(parts[split:])
            header = next(
                (i for i, line in enumerate(self.lines) if line.strip() == f"[{table}]"),
                None if table else -1,
            )
            if header is None:
                continue
            first = key.split(".")[0]
            for i in range(header + 1, len(self.lines)):
                stripped = self.lines[i].strip()
                if stripped.startswith("["):
                    break
                if re.match(rf"^\"?{re.escape(first)}\"?\s*=", stripped):
                    return i + 1
            return header + 1 if header >= 0 else None
        return None

    # ---- scalar checks -----------------------------------------------------------------------

    def table(self, value: object, key: str) -> Mapping[str, object]:
        if not isinstance(value, dict):
            raise self.fail("must be a table", key)
        return value

    def str_list(self, value: object, key: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.fail("must be a list of strings", key)
        return tuple(value)

    def domains(self, value: object, key: str) -> tuple[str, ...]:
        out = []
        for entry in self.str_list(value, key):
            name = entry.strip().rstrip(".").lower()
            try:
                name = name.encode("idna").decode("ascii") if not name.startswith("*.") else name
            except UnicodeError as err:
                raise self.fail(f"{entry!r} is not a valid host name", key) from err
            if not _DOMAIN_RE.match(name):
                raise self.fail(f"{entry!r} is not a host name or '*.' wildcard", key)
            out.append(name)
        return tuple(out)

    def verdict(self, value: object, key: str) -> Verdict:
        if value not in tuple(Verdict):
            raise self.fail("must be allow, ask or deny", key)
        return Verdict(value)

    def rate(self, value: object, key: str) -> Rate:
        if not isinstance(value, str):
            raise self.fail("must be a string like '30/h'", key)
        try:
            return Rate.parse(value)
        except ValueError as err:
            raise self.fail(str(err), key) from err

    # ---- tables --------------------------------------------------------------------------------

    def settings(self, data: Mapping[str, object], section: str) -> object:
        table = self.table(data.get(section, {}), section)
        spec = _SECTIONS[section]
        values: dict[str, object] = {}
        for key, value in table.items():
            dotted = f"{section}.{key}"
            if key not in spec:
                raise self.fail("unknown key", dotted)
            expected, check = spec[key]
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                name = expected.__name__ if isinstance(expected, type) else "number"
                raise self.fail(f"expected {name}", dotted)
            if check is not None and (problem := check(value)):
                raise self.fail(problem, dotted)
            if section == "rate":
                value = self.rate(value, dotted)
            elif section == "egress" and key in ("domains", "from_search_exclude"):
                value = self.domains(value, dotted)
            elif isinstance(value, list):
                value = self.str_list(value, dotted)
            elif section == "guard" and key == "threshold":
                value = float(value)
            values[key] = value
        return _SETTINGS_TYPES[section](**values)

    def rules(self, value: object, key: str, allowed: frozenset[str]) -> dict[str, Rule]:
        rules: dict[str, Rule] = {}
        for name, entry in self.table(value, key).items():
            rules[name] = self.rule(entry, f"{key}.{name}", allowed)
        return rules

    def rule(self, entry: object, key: str, allowed: frozenset[str]) -> Rule:
        table = self.table(entry, key)
        values: dict[str, object] = {}
        for name, value in table.items():
            dotted = f"{key}.{name}"
            if name not in allowed:
                raise self.fail("unknown key", dotted)
            if name in ("enabled", "reviewable"):
                if not isinstance(value, bool):
                    raise self.fail("expected bool", dotted)
                values[name] = value
            elif name == "verdict":
                values[name] = self.verdict(value, dotted)
            elif name == "rate":
                values[name] = self.rate(value, dotted)
            elif name == "domains":
                values[name] = self.domains(value, dotted)
            elif name in ("read_roots", "write_roots"):
                values[name] = self.str_list(value, dotted)
            elif name == "allow_args":
                values[name] = {
                    param: self.str_list(patterns, f"{dotted}.{param}")
                    for param, patterns in self.table(value, dotted).items()
                }
        return Rule(**values)

    def profiles(self, data: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
        custom = dict(self.table(data.get("profiles", {}), "profiles"))
        for name, entry in custom.items():
            key = f"profiles.{name}"
            table = self.table(entry, key)
            for item in table:
                if item not in _PROFILE_KEYS:
                    raise self.fail("unknown key", f"{key}.{item}")
            if name in PROFILES and "extends" in table:
                raise self.fail("a built-in profile cannot extend another", f"{key}.extends")
        return custom

    def resolve_profile(
        self, name: str, custom: Mapping[str, Mapping[str, object]], seen: tuple[str, ...] = ()
    ) -> tuple[Profile, dict[str, Rule], dict[str, Rule]]:
        """The profile `name` after `extends`, plus its user `tools` and `packs` tables merged."""
        if name in seen:
            raise self.fail(f"profile cycle: {' -> '.join((*seen, name))}", f"profiles.{seen[-1]}.extends")
        if name not in PROFILES and name not in custom:
            known = ", ".join([*PROFILES, *custom])
            where = f"profiles.{seen[-1]}.extends" if seen else "profile"
            raise self.fail(f"unknown profile {name!r}; profiles: {known}", where)
        table = custom.get(name, {})
        key = f"profiles.{name}"
        if name in PROFILES:
            base, tools, packs = PROFILES[name], {}, {}
        else:
            parent = table.get("extends", "default")
            if not isinstance(parent, str):
                raise self.fail("expected str", f"{key}.extends")
            base, tools, packs = self.resolve_profile(parent, custom, (*seen, name))
        tiers = dict(base.tiers)
        if "tiers" in table:
            for tier, verdict in self.table(table["tiers"], f"{key}.tiers").items():
                if tier not in tuple(Tier):
                    raise self.fail("unknown tier", f"{key}.tiers.{tier}")
                tiers[Tier(tier)] = self.verdict(verdict, f"{key}.tiers.{tier}")
            if tiers[Tier.SYSTEM] is Verdict.ALLOW:
                raise self.fail("the system tier can never be allow (REQ-SEC-08)", f"{key}.tiers.system")
        act_reversible = base.act_reversible
        if "act_reversible" in table:
            act_reversible = self.verdict(table["act_reversible"], f"{key}.act_reversible")
        reviewer = base.reviewer
        if "reviewer" in table:
            if not isinstance(table["reviewer"], bool):
                raise self.fail("expected bool", f"{key}.reviewer")
            reviewer = table["reviewer"]
        scopes = base.scopes
        if "scopes" in table:
            raw = self.str_list(table["scopes"], f"{key}.scopes")
            if not raw or not set(raw) <= {s.value for s in PROMPT_SCOPES}:
                raise self.fail(
                    "must be a non-empty subset of once, session; there is no always", f"{key}.scopes"
                )
            scopes = tuple(Scope(s) for s in dict.fromkeys(raw))
        tools = {**tools, **self.rules(table.get("tools", {}), f"{key}.tools", _TOOL_KEYS)}
        packs = {**packs, **self.rules(table.get("packs", {}), f"{key}.packs", _PACK_KEYS)}
        profile = replace(
            base,
            name=name,
            tiers=tiers,
            act_reversible=act_reversible,
            reviewer=reviewer,
            scopes=scopes,
        )
        return profile, tools, packs


def check_file_safety(path: Path) -> None:
    """Raises `ConfigError` when `path` is owned by another user or writable by group or others."""
    if os.name == "nt":
        return
    info = path.stat()
    if info.st_uid != os.getuid():
        raise ConfigError(f"owned by uid {info.st_uid}, not by you (REQ-SEC-15)", path=path, table="policy")
    if info.st_mode & UNSAFE_MODE_BITS:
        mode = info.st_mode & 0o777
        raise ConfigError(
            f"mode {mode:04o} lets others edit it; run: chmod 600 {path}", path=path, table="policy"
        )


def load_policy(
    path: Path | None = None, *, profile: str | None = None, channel: str | None = None
) -> Policy:
    """Reads `policy.toml` and resolves the active profile and channel.

    `profile` overrides the file's `profile` key; `channel` is `voice`, `telegram`, `remote` or
    None. Raises `ConfigError` (REQ-SEC-14, REQ-SEC-15) as the module header describes.
    """
    path = path if path is not None else halpaths.policy_file()
    data: Mapping[str, object] = {}
    text = ""
    if path.exists():
        check_file_safety(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        data = halconfig.read_toml(path)
    loader = _Loader(path, text)
    for key in data:
        if key not in _TOP_KEYS:
            raise loader.fail("unknown key", key)
    if data and data.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise loader.fail(f"must be {POLICY_SCHEMA_VERSION}", "schema_version")
    name = profile or data.get("profile") or "default"
    if not isinstance(name, str):
        raise loader.fail("expected str", "profile")
    if channel is not None and channel not in CHANNEL_PROFILES:
        raise loader.fail(f"unknown channel {channel!r}; channels: {', '.join(CHANNEL_PROFILES)}", "channel")
    custom = loader.profiles(data)
    active, profile_tools, profile_packs = loader.resolve_profile(name, custom)
    for other in custom:  # a broken profile that is not active is still a broken file
        if other != name:
            loader.resolve_profile(other, custom)
    settings = {section: loader.settings(data, section) for section in _SECTIONS}
    approval: ApprovalSettings = settings["approval"]
    egress: EgressSettings = settings["egress"]
    guard: GuardSettings = settings["guard"]
    channel_profile = CHANNEL_PROFILES.get(channel) if channel else None
    if channel_profile is not None and channel_profile.approval_timeout_s is not None:
        approval = replace(approval, timeout_s=channel_profile.approval_timeout_s)
    if active.egress_unlisted == "deny":
        egress = replace(egress, unlisted="deny")
    if active.guard_on_flag == "deny":
        guard = replace(guard, on_flag="deny")
    return Policy(
        profile=active,
        channel_profile=channel_profile,
        tools=loader.rules(data.get("tools", {}), "tools", _TOOL_KEYS),
        packs=loader.rules(data.get("packs", {}), "packs", _PACK_KEYS),
        profile_tools=profile_tools,
        profile_packs=profile_packs,
        approval=approval,
        audit=settings["audit"],
        guard=guard,
        reviewer=settings["reviewer"],
        paths=settings["paths"],
        egress=egress,
        rate=settings["rate"],
        path=path if data else None,
    )


def check_rules(policy: Policy, metas: Iterable[ToolMeta]) -> None:
    """Raises `ConfigError` for a user rule that breaks a REQ-SEC-08 invariant for a known tool.

    A `system` tool may not be set to `allow` or `reviewable` by name or through its pack; a
    `runs_code` tool may only be set to `ask`, and only by name.
    """
    where = policy.path or halpaths.policy_file()
    prefix = f"profiles.{policy.profile.name}."

    def refuse(key: str, why: str) -> ConfigError:
        return ConfigError(f"{key}: {why}", path=where, table="policy", key=key)

    for meta in metas:
        named = (
            ("tools", policy.tools.get(meta.name)),
            (prefix + "tools", policy.profile_tools.get(meta.name)),
        )
        packed = (
            ("packs", policy.packs.get(meta.pack)),
            (prefix + "packs", policy.profile_packs.get(meta.pack)),
        )
        for table, rule in (*named, *packed):
            if rule is None:
                continue
            key = f"{table}.{meta.name if table.endswith('tools') else meta.pack}"
            if meta.tier is Tier.SYSTEM and rule.verdict is Verdict.ALLOW:
                raise refuse(f"{key}.verdict", f"{meta.name} is a system tool and can never be allow")
            if meta.tier is Tier.SYSTEM and rule.reviewable:
                raise refuse(f"{key}.reviewable", f"{meta.name} is a system tool and is never reviewable")
            if meta.runs_code and rule.verdict is Verdict.ALLOW:
                raise refuse(f"{key}.verdict", f"{meta.name} runs code; only verdict = 'ask' by name")


def settings_fields(section: str) -> tuple[str, ...]:
    """The keys of one settings table, for `hal policy show` and the template test."""
    return tuple(f.name for f in fields(_SETTINGS_TYPES[section]))

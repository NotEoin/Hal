"""Phase-1 stand-in for 01-security's `halhub.policy`: a loader and a reduced `decide`.

04-hub's implementation plan lands the hub's call pipeline before 01-security's policy engine
(01's implementation plan is phase 2) and tests it "against their published interfaces with
fakes". Its types (`Tier` to `Decision`) now come from `halhub.policy.model`, so the stand-in
and the real engine share one `ToolMeta`. The rest copies 01-security.md, "Decision algorithm",
with the same names, fields and defaults, so phase 2 replaces it with an import change:
`Policy` with `Policy.tool`, `load_policy`, `prepare` and `decide`. It re-exports
`canonical_json` (`halkit.canon`), `approval_key` and `security_digest`
(`halhub.approvals.keys`) for the hub modules that still import them from here.

What `decide` implements, in 01's order: unknown tool, disabled tool, reserved-key stripping
and self-approval detection, argument validation with 01's `bad-args:<code>` codes, the
profile and channel verdict with 01's invariants (`system` never `allow`, `runs_code` denied
unless the user asks for it by name), URL scheme, userinfo and private-host checks, the
flagged-turn rule, grant matching, `no-approver` and the reviewable flag.

What is missing on purpose, each fail-closed or neutral until 01 lands:

- path roots, the hard-deny list and the workspace rule (REQ-SEC-09): `prepare` resolves
  paths and `decide` denies one that did not resolve, nothing more;
- domain allowlists, `egress.unlisted` and `url:from-search` (REQ-SEC-10, REQ-SEC-92): a URL
  that passes the scheme and host checks keeps the tier verdict, never a looser one;
- rate limits (REQ-SEC-12): `RateLimiter` counts calls, `decide` checks no limit;
- `allow_args`, custom `[profiles.<name>]` tables and per-profile overrides: the loader
  rejects those keys naming them, so a file that relies on them never runs with less policy
  than it asks for;
- `explain` and the trace beyond the rule ids `decide` returns.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from halkit import config as halconfig
from halkit import paths
from halkit.canon import canonical_json  # noqa: F401 - re-exported for hub modules until 01 step 20

from .approvals.keys import approval_key, security_digest
from .policy.model import (
    Context,
    Decision,
    Grant,
    Origin,
    ParamSpec,
    RateSnapshot,
    ResolvedPath,
    Scope,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    Verdict,
)

MAX_PATH_BYTES = 4096  # PATH_MAX on Linux; 01 `prepare` rejects longer paths
SELF_APPROVAL_RE = re.compile(r"(?i)^_*(confirm|approv|consent|authori[sz]|yes$|force$)")  # REQ-SEC-06
PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")  # REQ-SEC-10
POLICY_SCHEMA_VERSION = 1
CHANNELS = ("voice", "telegram", "remote")


# ---- settings ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalSettings:
    """`[approval]` of `policy.toml` (01 gives each default's reason)."""

    timeout_s: int = 120
    once_ttl_s: int = 600
    session_max_h: int = 12
    routine_max_days: int = 90
    notify: bool = True


@dataclass(frozen=True)
class AuditSettings:
    """`[audit]` of `policy.toml`."""

    content: bool = False
    args: bool = True
    retention_days: int = 90
    max_file_mb: int = 64


@dataclass(frozen=True)
class GuardSettings:
    """The `[guard]` keys the hub reads."""

    enabled: bool = False
    on_flag: Literal["ask", "deny"] = "ask"


@dataclass(frozen=True)
class ReviewerSettings:
    """The `[reviewer]` keys the hub reads."""

    enabled: bool = False
    when_untrusted: bool = True
    model: str = "hal-fast"
    timeout_s: int = 20


@dataclass(frozen=True)
class EgressSettings:
    """The `[egress]` keys the stand-in checks."""

    allow_http: bool = False
    allow_ip_literals: bool = False
    allow_private: bool = False


@dataclass(frozen=True)
class Rule:
    """One `[tools.<t>]` or `[packs.<p>]` entry; None leaves the key to the level below."""

    enabled: bool | None = None
    verdict: Verdict | None = None
    reviewable: bool | None = None


@dataclass(frozen=True)
class Profile:
    """A built-in profile: tier verdicts, the reversible-act verdict, reviewer, scopes, tools."""

    name: str
    tiers: Mapping[Tier, Verdict]
    act_reversible: Verdict
    reviewer: bool
    scopes: tuple[Scope, ...]
    tools: Mapping[str, Rule]


@dataclass(frozen=True)
class ToolPolicy:
    """A tool's resolved policy after precedence and invariants (01 `Policy.tool`)."""

    verdict: Verdict
    rule: str
    enabled: bool
    reviewable: bool


_DEFAULT_TIERS = {
    Tier.READ: Verdict.ALLOW,
    Tier.WRITE: Verdict.ALLOW,
    Tier.ACT: Verdict.ASK,
    Tier.SEND: Verdict.ASK,
    Tier.SYSTEM: Verdict.ASK,
}
# 01 "Built-in profiles": the C4 sends with destinations fixed in the user's config, and the
# tightenings carried from 08's per-tool table.
_DEFAULT_TOOLS = {
    "notify_send": Rule(verdict=Verdict.ALLOW),
    "briefing_deliver": Rule(verdict=Verdict.ALLOW),
    "calendar_sync": Rule(verdict=Verdict.ALLOW),
    "calendar_delete": Rule(verdict=Verdict.ASK),
    "tasks_delete": Rule(verdict=Verdict.ASK),
    "habits_remove": Rule(verdict=Verdict.ASK),
    "desktop_clipboard": Rule(verdict=Verdict.ASK),
}
_BOTH_SCOPES = (Scope.ONCE, Scope.SESSION)
PROFILES: Mapping[str, Profile] = {
    "default": Profile("default", _DEFAULT_TIERS, Verdict.ALLOW, True, _BOTH_SCOPES, _DEFAULT_TOOLS),
    "strict": Profile(
        "strict",
        {**_DEFAULT_TIERS, Tier.WRITE: Verdict.ASK, Tier.SYSTEM: Verdict.DENY},
        Verdict.ASK,
        False,
        (Scope.ONCE,),
        {
            **_DEFAULT_TOOLS,
            **{t: Rule(verdict=Verdict.ASK) for t in ("notify_send", "briefing_deliver", "calendar_sync")},
        },
    ),
    "trusted": Profile(
        "trusted",
        {**_DEFAULT_TIERS, Tier.ACT: Verdict.ALLOW},
        Verdict.ALLOW,
        True,
        _BOTH_SCOPES,
        _DEFAULT_TOOLS,
    ),
    "standalone": Profile(
        "standalone",
        {**_DEFAULT_TIERS, Tier.SYSTEM: Verdict.DENY},
        Verdict.ALLOW,
        True,
        _BOTH_SCOPES,
        _DEFAULT_TOOLS,
    ),
}
CHANNEL_TIMEOUTS = {"voice": 20, "telegram": 600}  # 01 "Channel profiles"
CHANNEL_DENIED_PACKS = {"remote": frozenset({"desktop"})}


@dataclass(frozen=True)
class Policy:
    """Resolved policy settings for one profile and channel (01 `Policy`)."""

    profile: Profile = PROFILES["default"]
    channel: str | None = None
    tools: Mapping[str, Rule] = field(default_factory=dict)
    packs: Mapping[str, Rule] = field(default_factory=dict)
    approval: ApprovalSettings = ApprovalSettings()
    audit: AuditSettings = AuditSettings()
    guard: GuardSettings = GuardSettings()
    reviewer: ReviewerSettings = ReviewerSettings()
    egress: EgressSettings = EgressSettings()
    path: Path | None = None  # the file read, None for built-ins only

    def tool(self, meta: ToolMeta) -> ToolPolicy:
        """The tool's verdict and rule after 01's precedence (REQ-SEC-07) and invariants (REQ-SEC-08)."""
        profile = self.profile
        verdict = profile.tiers[meta.tier]
        rule = f"tier:{meta.tier}" if verdict is not Verdict.DENY else f"tier:{meta.tier}@{profile.name}"
        if meta.tier is Tier.ACT and meta.reversible:
            verdict, rule = profile.act_reversible, "act-reversible"
        enabled, reviewable = True, meta.reviewable
        levels = (
            (profile.tools.get(meta.name), f"profile-tool:{meta.name}@{profile.name}"),
            (self.packs.get(meta.pack), f"pack:{meta.pack}"),
            (self.tools.get(meta.name), f"tool:{meta.name}"),
        )
        for entry, entry_rule in levels:
            if entry is None:
                continue
            if entry.verdict is not None:
                verdict, rule = entry.verdict, entry_rule
            if entry.enabled is not None:
                enabled = entry.enabled
            if entry.reviewable is not None:
                reviewable = entry.reviewable
        if meta.tier is Tier.SYSTEM:
            reviewable = False
            if verdict is Verdict.ALLOW:
                # 01's loader rejects this; the stand-in cannot see tiers at load time, so it clamps
                verdict, rule = Verdict.ASK, "system-never-allow"
        if meta.runs_code:
            user = self.tools.get(meta.name)
            if not (user and user.verdict is Verdict.ASK) or verdict is Verdict.DENY:
                verdict, rule = Verdict.DENY, "runs-code"
        if self.channel is not None:
            if meta.tier is Tier.SYSTEM:
                verdict, rule = Verdict.DENY, f"tier:system@{self.channel}"
            elif meta.pack in CHANNEL_DENIED_PACKS.get(self.channel, ()):
                verdict, rule = Verdict.DENY, f"pack:{meta.pack}@{self.channel}"
            elif meta.runs_code:
                verdict, rule = Verdict.DENY, f"runs-code@{self.channel}"
        return ToolPolicy(verdict, rule, enabled, reviewable)

    def allowed_scopes(self, meta: ToolMeta) -> tuple[Scope, ...]:
        """Scopes a prompt may offer: the profile's, only `once` for channels and `system` tools."""
        scopes = self.profile.scopes
        if self.channel is not None or meta.tier is Tier.SYSTEM:
            return tuple(s for s in scopes if s is Scope.ONCE)
        return scopes


class RateLimiter:
    """Counts calls per scope for `RateSnapshot`; the stand-in enforces no limit (see header)."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def record(self, meta: ToolMeta | None, *, asked: bool = False) -> None:
        """Counts one call (and one prompt when `asked`)."""
        with self._lock:
            self._counts["session"] += 1
            if asked:
                self._counts["asks"] += 1
            if meta is not None:
                self._counts[str(meta.tier)] += 1
                self._counts[f"tool:{meta.name}"] += 1

    def snapshot(self) -> RateSnapshot:
        """The counts so far."""
        with self._lock:
            return RateSnapshot(dict(self._counts))


# ---- approval binding ----------------------------------------------------------------------


# ---- loading -------------------------------------------------------------------------------

_SECTIONS: Mapping[str, Mapping[str, tuple[type | tuple[type, ...], tuple[object, ...] | None]]] = {
    "approval": {
        "timeout_s": (int, None),
        "once_ttl_s": (int, None),
        "session_max_h": (int, None),
        "routine_max_days": (int, None),
        "notify": (bool, None),
    },
    "audit": {
        "content": (bool, None),
        "args": (bool, None),
        "retention_days": (int, None),
        "max_file_mb": (int, None),
    },
    "guard": {"enabled": (bool, None), "on_flag": (str, ("ask", "deny"))},
    "reviewer": {
        "enabled": (bool, None),
        "when_untrusted": (bool, None),
        "model": (str, None),
        "timeout_s": (int, None),
    },
    "egress": {"allow_http": (bool, None), "allow_ip_literals": (bool, None), "allow_private": (bool, None)},
}
_SETTINGS_TYPES = {
    "approval": ApprovalSettings,
    "audit": AuditSettings,
    "guard": GuardSettings,
    "reviewer": ReviewerSettings,
    "egress": EgressSettings,
}
_RULE_KEYS = {"enabled": bool, "verdict": str, "reviewable": bool}


def load_policy(
    path: Path | None = None, *, profile: str | None = None, channel: str | None = None
) -> Policy:
    """Reads `policy.toml` (default `halkit.paths.policy_file()`; a missing file means built-ins).

    Raises `halkit.config.ConfigError` naming the file and key for a malformed file, an unknown
    key, an invalid value, an unknown profile or channel, and for any 01 key this stand-in does
    not implement (see the module header), so no front end runs with less policy than asked.
    """
    path = path if path is not None else paths.policy_file()
    data: Mapping[str, object] = {}
    if path.exists():
        data = halconfig.read_toml(path)

    def fail(message: str, key: str | None = None) -> halconfig.ConfigError:
        return halconfig.ConfigError(
            f"{key + ': ' if key else ''}{message}", path=path, table="policy", key=key
        )

    allowed = {"schema_version", "profile", "tools", "packs", *_SECTIONS}
    for key in data:
        if key not in allowed:
            raise fail("unknown or not supported by the phase-1 policy stand-in", key)
    if data and data.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise fail(f"must be {POLICY_SCHEMA_VERSION}", "schema_version")
    name = profile or data.get("profile") or "default"
    if not isinstance(name, str) or name not in PROFILES:
        raise fail(f"unknown profile {name!r}; built-in profiles: {', '.join(PROFILES)}", "profile")
    if channel is not None and channel not in CHANNELS:
        raise fail(f"unknown channel {channel!r}; channels: {', '.join(CHANNELS)}", "channel")
    settings = {section: _settings(data, section, fail) for section in _SECTIONS}
    approval = settings["approval"]
    if channel in CHANNEL_TIMEOUTS:
        approval = replace(approval, timeout_s=CHANNEL_TIMEOUTS[channel])
    return Policy(
        profile=PROFILES[name],
        channel=channel,
        tools=_rules(data, "tools", fail),
        packs=_rules(data, "packs", fail),
        approval=approval,
        audit=settings["audit"],
        guard=settings["guard"],
        reviewer=settings["reviewer"],
        egress=settings["egress"],
        path=path if data else None,
    )


def _settings(data: Mapping[str, object], section: str, fail) -> object:
    table = data.get(section, {})
    if not isinstance(table, dict):
        raise fail("must be a table", section)
    values = {}
    for key, value in table.items():
        dotted = f"{section}.{key}"
        if key not in _SECTIONS[section]:
            raise fail("unknown or not supported by the phase-1 policy stand-in", dotted)
        expected, choices = _SECTIONS[section][key]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise fail(f"expected {expected.__name__}", dotted)
        if choices is not None and value not in choices:
            raise fail(f"must be one of {', '.join(map(str, choices))}", dotted)
        values[key] = value
    return _SETTINGS_TYPES[section](**values)


def _rules(data: Mapping[str, object], section: str, fail) -> dict[str, Rule]:
    table = data.get(section, {})
    if not isinstance(table, dict):
        raise fail("must be a table", section)
    rules = {}
    for name, entry in table.items():
        if not isinstance(entry, dict):
            raise fail("must be a table", f"{section}.{name}")
        for key, value in entry.items():
            dotted = f"{section}.{name}.{key}"
            if key not in _RULE_KEYS:
                raise fail("unknown or not supported by the phase-1 policy stand-in", dotted)
            if not isinstance(value, _RULE_KEYS[key]):
                raise fail(f"expected {_RULE_KEYS[key].__name__}", dotted)
            if key == "verdict" and value not in tuple(Verdict):
                raise fail("must be allow, ask or deny", dotted)
        verdict = entry.get("verdict")
        rules[name] = Rule(
            enabled=entry.get("enabled"),
            verdict=Verdict(verdict) if verdict is not None else None,
            reviewable=entry.get("reviewable"),
        )
    return rules


# ---- prepare and decide --------------------------------------------------------------------


def prepare(call: ToolCall, meta: ToolMeta, *, home: Path) -> Mapping[str, ResolvedPath]:
    """Resolves every `format = "path"` argument: `~/` expanded, relative, NUL and long paths refused."""
    resolved: dict[str, ResolvedPath] = {}
    for p in meta.params:
        value = call.args.get(p.name)
        if p.format != "path" or not isinstance(value, str):
            continue
        if "\x00" in value:
            resolved[p.name] = ResolvedPath(value, None, "nul")
            continue
        text = str(home) + value[1:] if value == "~" or value.startswith("~/") else value
        if len(os.fsencode(text)) > MAX_PATH_BYTES:
            resolved[p.name] = ResolvedPath(value, None, "too_long")
        elif not os.path.isabs(text):
            resolved[p.name] = ResolvedPath(value, None, "relative")
        else:
            try:
                resolved[p.name] = ResolvedPath(value, Path(os.path.realpath(text)), None)
            except (OSError, ValueError):
                resolved[p.name] = ResolvedPath(value, None, "unresolvable")
    return resolved


@dataclass(frozen=True)
class _ArgError:
    code: str
    message: str


def _deny(rule: str, reason: str, **kwargs: object) -> Decision:
    defaults: dict[str, object] = {"key": None, "args": {}, "stripped": (), "self_approval": ()}
    defaults.update(kwargs)
    return Decision(Verdict.DENY, rule, reason, **defaults)


def decide(call: ToolCall, meta: ToolMeta | None, policy: Policy, ctx: Context) -> Decision:
    """01's pure decision for one call, reduced as the module header lists; the first rule wins."""
    if meta is None:
        return _deny("unknown-tool", f"No tool named {call.tool}.")
    tp = policy.tool(meta)
    if not tp.enabled:
        return _deny("disabled", f"{meta.name} is disabled in policy.")
    args = {k: v for k, v in call.args.items() if not str(k).startswith("_")}
    stripped = tuple(sorted(str(k) for k in call.args if str(k).startswith("_")))
    known = {p.name for p in meta.params}
    suspicious = tuple(
        sorted(
            k
            for k in map(str, call.args)
            if (k.startswith("_") or k not in known) and SELF_APPROVAL_RE.match(k)
        )
    )
    err = _validate(meta, args)
    if err:
        return _deny(f"bad-args:{err.code}", err.message, stripped=stripped, self_approval=suspicious)
    for p in meta.params:
        if p.format == "path" and p.name in args:
            rp = ctx.paths.get(p.name)
            if rp is None or rp.resolved is None:
                why = rp.error if rp else "unresolved"
                return _deny(
                    "path:invalid",
                    f"{p.name}: the path is {why}.",
                    stripped=stripped,
                    self_approval=suspicious,
                )
            args[p.name] = str(rp.resolved)
    key = approval_key(meta.pack, meta.name, args)
    common = {"key": key, "args": args, "stripped": stripped, "self_approval": suspicious}
    if tp.verdict is Verdict.DENY:
        return _deny(
            tp.rule, f"{meta.name} is denied by profile {policy.profile.name} ({tp.rule}).", **common
        )
    for p in meta.params:
        if p.format in ("url", "host") and isinstance(args.get(p.name), str):
            problem = _check_url(args[p.name], p.format, policy.egress)
            if problem:
                return _deny(problem[0], f"{p.name}: {problem[1]}", **common)
    verdict, rule, fresh_only = tp.verdict, tp.rule, False
    if ctx.taint is Taint.FLAGGED and meta.tier in (Tier.ACT, Tier.SEND, Tier.SYSTEM):
        if policy.guard.on_flag == "deny":
            return _deny("flagged-turn", "This turn read flagged content, so this call is denied.", **common)
        verdict = Verdict.ASK if verdict is Verdict.ALLOW else verdict
        rule, fresh_only = "flagged-turn", True
    if verdict is Verdict.ALLOW:
        return Decision(Verdict.ALLOW, rule, f"{meta.name} is allowed ({rule}).", **common)
    if not fresh_only:
        grant = _match_grant(ctx.grants, key, meta, call, ctx)
        if grant is not None:
            return Decision(
                Verdict.ALLOW,
                f"grant:{grant.id}",
                f"{meta.name} is covered by grant {grant.id}.",
                grant_id=grant.id,
                **common,
            )
    if not ctx.approver_available:
        return _deny(
            "no-approver", f"{meta.name} needs approval and no approval surface is available.", **common
        )
    reviewable = (
        tp.reviewable
        and policy.reviewer.enabled
        and policy.profile.reviewer
        and meta.tier not in (Tier.SEND, Tier.SYSTEM)
        and not any(p.content for p in meta.params)
        and ctx.taint is not Taint.FLAGGED
        and (ctx.taint is Taint.CLEAN or policy.reviewer.when_untrusted)
    )
    return Decision(
        Verdict.ASK,
        rule,
        f"{meta.name} needs approval ({rule}).",
        reviewable=reviewable,
        scopes=policy.allowed_scopes(meta),
        fresh_only=fresh_only,
        **common,
    )


def _validate(meta: ToolMeta, args: Mapping[str, object]) -> _ArgError | None:
    by_name = {p.name: p for p in meta.params}
    for name in args:
        if name not in by_name:
            accepted = ", ".join(sorted(by_name)) or "none"
            return _ArgError("unknown", f"{meta.name}: unknown argument {name!r}; accepted: {accepted}")
    for p in meta.params:
        if p.name not in args:
            if p.required:
                return _ArgError("missing", f"{meta.name}: {p.name!r} is required")
            continue
        value = args[p.name]
        if not _type_ok(value, p.type):
            return _ArgError("type", f"{meta.name}: {p.name!r} must be {p.type}, got {type(value).__name__}")
        if p.enum is not None and value not in p.enum:
            choices = ", ".join(map(str, p.enum))
            return _ArgError("enum", f"{meta.name}: {p.name!r} must be one of {choices}")
        if isinstance(value, str):
            problem = _string_problem(p, value)
            if problem:
                return _ArgError(problem[0], f"{meta.name}: {p.name!r} {problem[1]}")
    return None


def _type_ok(value: object, ptype: str) -> bool:
    if ptype == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if ptype == "integer":
        return isinstance(value, int)
    if ptype == "number":
        return isinstance(value, int | float)
    return isinstance(value, str)


def _string_problem(p: ParamSpec, value: str) -> tuple[str, str] | None:
    if len(value) > p.max_length:
        return "length", f"is {len(value)} characters, the limit is {p.max_length}"
    if "\x00" in value:
        return "nul", "contains a NUL character"
    if p.format == "json":
        try:
            json.loads(value)
        except ValueError:
            return "json", "must be valid JSON"
    if p.format == "command" and ("\n" in value or "\r" in value):
        return "command", "may not contain a newline"
    if p.format in ("url", "host", "email", "phone") and value.startswith("-"):
        return "option", "may not start with '-'"  # REQ-SEC-56: never read as a program option
    return None


def _check_url(value: str, fmt: str, egress: EgressSettings) -> tuple[str, str] | None:
    if fmt == "url":
        parts = urlsplit(value)
        schemes = ("https", "http") if egress.allow_http else ("https",)
        if parts.scheme.lower() not in schemes:
            return (
                "url:scheme",
                f"scheme {parts.scheme or 'none'!r} is not allowed (only {', '.join(schemes)})",
            )
        if "@" in parts.netloc:
            return "url:userinfo", "URLs with a user name or password are not allowed"
        host = (parts.hostname or "").lower()
    else:
        host = value.lower()
    if not host:
        return "url:host", "has no host"
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return None if egress.allow_ip_literals else ("url:ip-literal", f"IP address {host} is not allowed")
    if not egress.allow_private and (
        host == "localhost" or host.endswith(PRIVATE_SUFFIXES) or "." not in host
    ):
        return "url:private", f"host {host} is a local or private name"
    return None


def _match_grant(
    grants: tuple[Grant, ...], key: str, meta: ToolMeta, call: ToolCall, ctx: Context
) -> Grant | None:
    digest = security_digest(meta)
    for g in grants:
        if g.expires <= ctx.now:
            continue
        if g.scope in (Scope.ONCE, Scope.SESSION):
            if g.scope is Scope.ONCE and g.used:
                continue
            same = (g.key, g.tool, g.pack, g.session, g.meta_sha256) == (
                key,
                meta.name,
                meta.pack,
                call.session,
                digest,
            )
            if same:
                return g
        elif (
            call.origin is Origin.ROUTINE
            and g.routine_id == call.routine_id
            and g.steps_sha256 is not None
            and g.steps_sha256 == ctx.routine_steps_sha256
            and g.step_keys.get(key) == digest
        ):
            return g
    return None

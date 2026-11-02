"""Types of the tool policy engine: tiers, verdicts, calls, grants, decisions and settings.

These are 01-security's "Decision algorithm" types, with the same names, fields and defaults,
plus the resolved forms of every `policy.toml` table ("`policy.toml` schema"). Everything here
is a frozen dataclass or an enum, so a `Decision` can be compared, hashed into tests and
handed between threads without copying.

Two classes are both called `Decision`: `halhub.policy.Decision` (this module) is what the
policy says about a call; `halhub.approvals.Decision` is a human's or reviewer's answer to an
approval request (REQ-SEC-03).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Literal

DEFAULT_MAX_LENGTH = 65536  # REQ-SEC-05: default string limit, characters
RATE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}  # 01 "Rate syntax"
_RATE_RE = re.compile(r"^([1-9][0-9]{0,6})/([smhd])$")


class Tier(StrEnum):
    """A tool's tier; what each one means for policy is 01-security's tier table."""

    READ = "read"
    WRITE = "write"
    ACT = "act"
    SEND = "send"
    SYSTEM = "system"


class Verdict(StrEnum):
    """What policy says about a call."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Taint(IntEnum):
    """A session's taint level (REQ-SEC-32); higher is less trusted."""

    CLEAN = 0
    UNTRUSTED = 1
    FLAGGED = 2

    @property
    def label(self) -> str:
        """The lowercase name the audit log and approval requests use."""
        return self.name.lower()


class Origin(StrEnum):
    """Where a call came from; set by the host from the socket or process, never by the caller."""

    TUI = "tui"
    BOX_AGENT = "box_agent"
    BOX_HARNESS = "box_harness"
    HOST_MCP = "host_mcp"
    ACP = "acp"
    ROUTINE = "routine"
    CLI = "cli"
    REMOTE = "remote"


class Scope(StrEnum):
    """A grant's scope; there is no "always" (RESOLUTIONS B2)."""

    ONCE = "once"
    SESSION = "session"
    ROUTINE = "routine"


ParamType = Literal["string", "integer", "number", "boolean"]
ParamFormat = Literal["text", "path", "url", "host", "email", "phone", "command", "json"]
Access = Literal["read", "write"]
SessionKind = Literal["code", "box", "chat", "headless", "routine", "remote", "hub"]


@dataclass(frozen=True)
class ParamSpec:
    """The parameter facts `decide` validates (type, enum, length, format, access, content)."""

    name: str
    type: ParamType = "string"
    required: bool = False
    enum: tuple[object, ...] | None = None
    max_length: int = DEFAULT_MAX_LENGTH
    format: ParamFormat = "text"
    access: Access = "read"
    content: bool = False


@dataclass(frozen=True)
class ToolMeta:
    """Everything the policy reads about one tool (REQ-SEC-02); built from manifest v2."""

    name: str  # [a-z][a-z0-9_]{0,63}
    pack: str  # 08 pack name, e.g. "comms"; module name if path-scanned
    pack_version: str
    tier: Tier
    params: tuple[ParamSpec, ...]
    network: bool = False
    untrusted_output: bool = False
    reviewable: bool = False
    reversible: bool = False
    runs_code: bool = False
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    """One call as the host received it; `session` and `origin` come from the host, not the caller."""

    tool: str
    args: Mapping[str, object]  # as received
    session: str  # 12-hex session id, from the socket
    origin: Origin
    call_id: str  # "c_" + 16 hex
    routine_id: str | None = None
    channel: str | None = None  # "voice" | "telegram" | "remote" | None
    remote_client: str | None = None  # <client> of --origin remote:<client> (07-remote)
    remote_addr: str | None = None  # from HAL_REMOTE_ADDR, audit only


@dataclass(frozen=True)
class ResolvedPath:
    """A `format = "path"` argument after `prepare`; `resolved` is None when it failed."""

    original: str
    resolved: Path | None
    error: str | None  # "relative", "nul", "too_long", "unresolvable"


@dataclass(frozen=True)
class RateSnapshot:
    """Calls per rate scope in the current window: `session`, `asks`, tiers, `tool:<name>`."""

    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Grant:
    """A standing approval for one key (`once`, `session`) or a routine's steps (`routine`)."""

    id: str  # "g_" + 16 hex
    scope: Scope
    key: str | None  # "sha256:<hex>" for ONCE and SESSION; None for ROUTINE
    pack: str | None  # None for ROUTINE
    tool: str | None  # None for ROUTINE
    session: str | None  # ONCE and SESSION
    routine_id: str | None  # ROUTINE
    steps_sha256: str | None  # ROUTINE: sha256 hex of canonical_json(steps)
    step_keys: Mapping[str, str]  # ROUTINE: key of each ask step -> meta_sha256 of its tool
    meta_sha256: str | None  # ONCE and SESSION: security_digest(meta) when granted
    created: datetime
    expires: datetime  # every scope expires (REQ-SEC-21)
    used: bool  # for ONCE
    source: str  # an Approver name or "reviewer"


@dataclass(frozen=True)
class Context:
    """The impure facts `decide` reads, gathered by the host before the decision."""

    now: datetime
    session_kind: SessionKind
    taint: Taint
    grants: tuple[Grant, ...]
    rate: RateSnapshot
    approver_available: bool
    paths: Mapping[str, ResolvedPath]  # param name -> resolution, filled by prepare()
    workspaces: tuple[Path, ...]  # resolved roots of every active session workspace
    home: Path
    routine_steps_sha256: str | None = None
    search_urls: frozenset[str] = frozenset()  # canonical URLs from search_tools results


@dataclass(frozen=True)
class Decision:
    """The policy's answer for one call; `args` are exactly what may run (REQ-SEC-18)."""

    verdict: Verdict
    rule: str  # stable id, e.g. "tier:act", "grant:g_...", "path:hard-deny"
    reason: str  # one sentence for the UI and the audit log
    key: str | None  # approval key, None when args were invalid
    args: Mapping[str, object]  # validated arguments, with resolved paths
    stripped: tuple[str, ...]  # removed "_" keys
    self_approval: tuple[str, ...]  # stripped or unknown keys that look like approvals
    reviewable: bool = False
    scopes: tuple[Scope, ...] = ()
    grant_id: str | None = None
    fresh_only: bool = False  # flagged turn: grants were not considered
    trace: tuple[str, ...] = ()  # rules evaluated, for `hal policy explain`


# ---- settings: the resolved tables of policy.toml -------------------------------------------


@dataclass(frozen=True)
class Rate:
    """A sliding-window limit: at most `count` calls per `window_s` seconds."""

    count: int
    window_s: int

    @classmethod
    def parse(cls, text: str) -> Rate:
        """Parses `"<count>/<unit>"` with unit `s`, `m`, `h` or `d`; raises `ValueError` otherwise."""
        match = _RATE_RE.match(text.strip())
        if not match:
            raise ValueError(f"{text!r} is not a rate like '30/h' (units s, m, h, d)")
        return cls(int(match.group(1)), RATE_UNITS[match.group(2)])

    def __str__(self) -> str:
        unit = next(u for u, s in RATE_UNITS.items() if s == self.window_s)
        return f"{self.count}/{unit}"


@dataclass(frozen=True)
class ApprovalSettings:
    """`[approval]`; 01 gives each default's reason."""

    timeout_s: int = 120
    once_ttl_s: int = 600
    session_max_h: int = 12
    routine_max_days: int = 90
    notify: bool = True


@dataclass(frozen=True)
class AuditSettings:
    """`[audit]`."""

    content: bool = False
    args: bool = True
    retention_days: int = 90
    max_file_mb: int = 64


@dataclass(frozen=True)
class GuardSettings:
    """`[guard]`."""

    enabled: bool = False
    model: Literal["prompt-guard-2-86m", "prompt-guard-2-22m"] = "prompt-guard-2-86m"
    threshold: float = 0.5
    max_scan_chars: int = 24000
    timeout_s: int = 15
    threads: int = 2
    on_flag: Literal["ask", "deny"] = "ask"
    backend: Literal["auto", "docker", "bwrap"] = "auto"


@dataclass(frozen=True)
class ReviewerSettings:
    """`[reviewer]`."""

    enabled: bool = False
    model: str = "hal-fast"
    timeout_s: int = 20
    when_untrusted: bool = True


Paranoia = Literal["off", "low", "balanced", "high", "max"]


@dataclass(frozen=True)
class ClassifierSettings:
    """`[policy]`: the risk classifier (RESOLUTIONS I5); `halhub.classifier` maps risk to effect."""

    paranoia: Paranoia = "balanced"
    model: str = "hal-fast"
    timeout_s: int = 20
    # False skips the model for a read-tier call that no deterministic rule flagged (the name says
    # "classify reads with the model": the default is the fast path)
    classify_reads: bool = False


DEFAULT_READ_ROOTS = ("~/Documents", "~/Downloads", "~/Desktop", "~/Pictures", "~/Music", "~/Videos")
DEFAULT_WRITE_ROOTS = ("~/Downloads",)


@dataclass(frozen=True)
class PathSettings:
    """`[paths]`; values keep their `~/` until a check expands them against the user's home."""

    read_roots: tuple[str, ...] = DEFAULT_READ_ROOTS
    write_roots: tuple[str, ...] = DEFAULT_WRITE_ROOTS
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class EgressSettings:
    """`[egress]`."""

    domains: tuple[str, ...] = ()
    unlisted: Literal["ask", "deny"] = "ask"
    allow_http: bool = False
    allow_ip_literals: bool = False
    allow_private: bool = False
    max_redirects: int = 3
    search_tools: tuple[str, ...] = ("web_search",)
    from_search: tuple[str, ...] = ("web_read",)
    from_search_exclude: tuple[str, ...] = ("docs.google.com", "forms.gle")


@dataclass(frozen=True)
class RateSettings:
    """`[rate]`: per-session limits for all calls, prompts and the three costly tiers."""

    session: Rate = Rate(2000, 3600)
    asks: Rate = Rate(40, 3600)
    act: Rate = Rate(120, 3600)
    send: Rate = Rate(30, 3600)
    system: Rate = Rate(10, 3600)


@dataclass(frozen=True)
class Rule:
    """One `[tools.<t>]` or `[packs.<p>]` entry; None leaves the key to the level below."""

    enabled: bool | None = None
    verdict: Verdict | None = None
    reviewable: bool | None = None
    rate: Rate | None = None  # tools only
    read_roots: tuple[str, ...] | None = None  # tools only
    write_roots: tuple[str, ...] | None = None  # tools only
    domains: tuple[str, ...] | None = None
    allow_args: Mapping[str, tuple[str, ...]] | None = None  # tools only


@dataclass(frozen=True)
class Profile:
    """A built-in or custom profile after `extends` is resolved."""

    name: str
    tiers: Mapping[Tier, Verdict]
    act_reversible: Verdict
    reviewer: bool
    scopes: tuple[Scope, ...]
    tools: Mapping[str, Rule] = field(default_factory=dict)
    packs: Mapping[str, Rule] = field(default_factory=dict)
    # strict's settings beyond tier verdicts (01 "Built-in profiles"); None keeps the file's value
    egress_unlisted: Literal["ask", "deny"] | None = None
    guard_on_flag: Literal["ask", "deny"] | None = None
    sandbox_require: Literal["any", "container"] = "any"  # 02-sandbox [sandbox].require, REQ-SEC-102


@dataclass(frozen=True)
class ChannelProfile:
    """A channel's tightenings, applied after every other level; it can never loosen."""

    name: str
    denied_tiers: frozenset[Tier]
    denied_packs: frozenset[str]
    scopes: tuple[Scope, ...]
    approval_timeout_s: int | None


@dataclass(frozen=True)
class ToolPolicy:
    """A tool's resolved policy after precedence and invariants (`Policy.tool`)."""

    verdict: Verdict
    rule: str
    enabled: bool
    reviewable: bool
    rate: Rate | None = None
    read_roots: tuple[str, ...] = DEFAULT_READ_ROOTS
    write_roots: tuple[str, ...] = DEFAULT_WRITE_ROOTS
    domains: tuple[str, ...] = ()
    allow_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    """Resolved `policy.toml` for one profile and channel (01-security, "Decision algorithm").

    `tool(meta)` applies REQ-SEC-07's precedence (later wins): the profile's tier table, its
    `act_reversible` verdict and its built-in per-tool entries; `[packs.<p>]`; `[tools.<t>]`;
    `[profiles.<active>.packs.<p>]`; `[profiles.<active>.tools.<t>]`; then the channel profile,
    which only tightens. REQ-SEC-08's invariants are applied last, so even a policy built in
    code cannot let a `system` tool resolve to `allow` or a `runs_code` tool escape `deny`;
    the loader also rejects files that try (see `halhub.policy.load`).
    """

    profile: Profile
    channel_profile: ChannelProfile | None = None
    tools: Mapping[str, Rule] = field(default_factory=dict)
    packs: Mapping[str, Rule] = field(default_factory=dict)
    profile_tools: Mapping[str, Rule] = field(default_factory=dict)  # [profiles.<active>.tools]
    profile_packs: Mapping[str, Rule] = field(default_factory=dict)  # [profiles.<active>.packs]
    approval: ApprovalSettings = ApprovalSettings()
    audit: AuditSettings = AuditSettings()
    guard: GuardSettings = GuardSettings()
    reviewer: ReviewerSettings = ReviewerSettings()
    classifier: ClassifierSettings = ClassifierSettings()
    paths: PathSettings = PathSettings()
    egress: EgressSettings = EgressSettings()
    rate: RateSettings = RateSettings()
    path: Path | None = None  # the file read; None for built-ins only
    # facts `check_path` needs from the file system, recorded by `load_policy` (see policy.paths)
    hal_dirs: tuple[Path, ...] = ()  # state, config, data and runtime dirs, $XDG_RUNTIME_DIR
    real_paths: Mapping[Path, Path] = field(default_factory=dict)  # configured path -> realpath
    # init=False: `dataclasses.replace` must start a new memo, never share the old policy's
    _memo: dict[tuple[object, ...], ToolPolicy] = field(
        default_factory=dict, init=False, compare=False, repr=False, hash=False
    )

    @property
    def channel(self) -> str | None:
        """The channel profile's name, or None outside a channel."""
        return self.channel_profile.name if self.channel_profile else None

    @property
    def sandbox_require(self) -> Literal["any", "container"]:
        """02-sandbox `[sandbox].require` as this profile sets it (`strict`: container, REQ-SEC-102)."""
        return self.profile.sandbox_require

    def tool(self, meta: ToolMeta) -> ToolPolicy:
        """The tool's verdict, rule and settings after precedence and invariants; memoised."""
        memo_key = (
            meta.pack,
            meta.name,
            meta.tier,
            meta.reversible,
            meta.runs_code,
            meta.reviewable,
            meta.domains,
        )
        cached = self._memo.get(memo_key)
        if cached is None:
            cached = self._memo[memo_key] = self._resolve(meta)
        return cached

    def _resolve(self, meta: ToolMeta) -> ToolPolicy:
        profile = self.profile
        verdict = profile.tiers[meta.tier]
        rule = f"tier:{meta.tier}" if verdict is not Verdict.DENY else f"tier:{meta.tier}@{profile.name}"
        if meta.tier is Tier.ACT and meta.reversible:
            verdict, rule = profile.act_reversible, "act-reversible"
        enabled, reviewable = True, meta.reviewable
        rate: Rate | None = None
        read_roots, write_roots = self.paths.read_roots, self.paths.write_roots
        domains, allow_args = meta.domains, {}
        user_named_ask = False  # REQ-SEC-08: only a by-name `ask` lets a runs_code tool run
        levels = (
            (profile.tools.get(meta.name), f"profile-tool:{meta.name}@{profile.name}", False),
            (self.packs.get(meta.pack), f"pack:{meta.pack}", False),
            (self.tools.get(meta.name), f"tool:{meta.name}", True),
            (self.profile_packs.get(meta.pack), f"profile-pack:{meta.pack}@{profile.name}", False),
            (self.profile_tools.get(meta.name), f"profile-tool:{meta.name}@{profile.name}", True),
        )
        for entry, entry_rule, by_name in levels:
            if entry is None:
                continue
            if entry.verdict is not None:
                verdict, rule = entry.verdict, entry_rule
                user_named_ask = by_name and entry.verdict is Verdict.ASK
            if entry.enabled is not None:
                enabled = entry.enabled
            if entry.reviewable is not None:
                reviewable = entry.reviewable
            if entry.rate is not None:
                rate = entry.rate
            if entry.read_roots is not None:
                read_roots = entry.read_roots
            if entry.write_roots is not None:
                write_roots = entry.write_roots
            if entry.domains is not None:
                domains = entry.domains
            if entry.allow_args is not None:
                allow_args = dict(entry.allow_args)
        if meta.tier is Tier.SYSTEM:
            reviewable = False
            if verdict is Verdict.ALLOW:
                verdict, rule = Verdict.ASK, "system-never-allow"
        if meta.runs_code and not (user_named_ask and verdict is Verdict.ASK):
            verdict, rule = Verdict.DENY, "runs-code"
        channel = self.channel_profile
        if channel is not None and verdict is not Verdict.DENY:
            if meta.tier in channel.denied_tiers:
                verdict, rule = Verdict.DENY, f"tier:{meta.tier}@{channel.name}"
            elif meta.pack in channel.denied_packs:
                verdict, rule = Verdict.DENY, f"pack:{meta.pack}@{channel.name}"
            elif meta.runs_code:
                verdict, rule = Verdict.DENY, f"runs-code@{channel.name}"
        return ToolPolicy(
            verdict=verdict,
            rule=rule,
            enabled=enabled,
            reviewable=reviewable,
            rate=rate,
            read_roots=read_roots,
            write_roots=write_roots,
            domains=domains,
            allow_args=allow_args,
        )

    def allowed_scopes(self, meta: ToolMeta) -> tuple[Scope, ...]:
        """Scopes a prompt may offer: the profile's, only `once` in a channel or for `system` tools."""
        scopes = tuple(s for s in self.profile.scopes if s is not Scope.ROUTINE)
        if self.channel_profile is not None:
            scopes = tuple(s for s in scopes if s in self.channel_profile.scopes)
        if meta.tier is Tier.SYSTEM:
            scopes = tuple(s for s in scopes if s is Scope.ONCE)
        return scopes

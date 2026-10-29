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

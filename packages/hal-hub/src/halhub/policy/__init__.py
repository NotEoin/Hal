"""The tool policy engine: tiers, profiles, `policy.toml`, and the pure decision for each call.

01-security owns the design ("Decision algorithm", "`policy.toml` schema"). The hub calls
`prepare` and then `decide` for every call on every front end.
"""

from .decide import decide, explain
from .load import ConfigError, check_rules, load_policy
from .model import (
    ApprovalSettings,
    AuditSettings,
    ChannelProfile,
    ClassifierSettings,
    Context,
    Decision,
    EgressSettings,
    Grant,
    GuardSettings,
    Origin,
    ParamSpec,
    PathSettings,
    Policy,
    Profile,
    Rate,
    RateSettings,
    RateSnapshot,
    ResolvedPath,
    ReviewerSettings,
    Rule,
    Scope,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    ToolPolicy,
    Verdict,
)
from .paths import check_host_path, prepare, walk_policy
from .profiles import CHANNEL_PROFILES, PROFILES
from .workspaces import active_workspaces

__all__ = [
    "active_workspaces",
    "check_host_path",
    "prepare",
    "walk_policy",
    "decide",
    "explain",
    "check_rules",
    "load_policy",
    "CHANNEL_PROFILES",
    "PROFILES",
    "ApprovalSettings",
    "AuditSettings",
    "ChannelProfile",
    "ClassifierSettings",
    "ConfigError",
    "Context",
    "Decision",
    "EgressSettings",
    "Grant",
    "GuardSettings",
    "Origin",
    "ParamSpec",
    "PathSettings",
    "Policy",
    "Profile",
    "Rate",
    "RateSettings",
    "RateSnapshot",
    "ResolvedPath",
    "ReviewerSettings",
    "Rule",
    "Scope",
    "Taint",
    "Tier",
    "ToolCall",
    "ToolMeta",
    "ToolPolicy",
    "Verdict",
]

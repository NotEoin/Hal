"""The pure policy decision for one tool call (01-security, "Decision algorithm").

`decide(call, meta, policy, ctx)` reads only its arguments: no file, socket, clock or
environment (REQ-SEC-03). Everything impure (path resolution, the time, grants, rate counts,
taint, whether an approval surface is attached) is gathered by the host into `Context` first.
The first rule that returns wins, and every outcome carries a stable rule id, a one-sentence
reason and the trace of rules evaluated (REQ-SEC-16), which `explain` renders for
`hal policy explain`.

Order: unknown tool, disabled tool, `_` key stripping and self-approval detection (REQ-SEC-06),
argument validation (REQ-SEC-05, REQ-SEC-56), resolved paths, the approval key, the resolved
verdict's deny, `runs_code` in a box session (REQ-SEC-59), path rules, `allow_args`, the
flagged-turn rule (REQ-SEC-33), allow, grants (REQ-SEC-21), `no-approver` (REQ-SEC-13) and
the reviewable flag (REQ-SEC-37). Path rules (REQ-SEC-09) live in `halhub.policy.paths`.

URL and host arguments (REQ-SEC-10) are checked with `halkit.netguard.check_url`, which never
resolves a name; a listed host turns `ask` into `allow` (`domain-allowlist`, never for
`system` tools), and an unlisted host turns `allow` into `ask`, or denies when
`[egress].unlisted = "deny"`. A tool in `[egress].from_search` whose every URL argument is
canonically equal to a search-result URL of this session (`Context.search_urls`, recorded by
`halhub.policy.provenance`) turns `ask` into `allow` with `url:from-search` (REQ-SEC-92),
except for `system` tools and hosts on `[egress].from_search_exclude`; the flagged-turn rule
runs after it, so a flagged turn still asks.

Rate limits (REQ-SEC-12) come after the URL checks: `Context.rate` is a snapshot taken by
`halhub.policy.rate.RateLimiter`, and `check_rate` compares it with `[rate]` and the tool's own
`rate`. The prompt budget (`rate:asks`) is checked only when the call would raise a prompt,
after grants, so a granted call never counts against it.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from halkit import netguard

from ..approvals.keys import approval_key, security_digest
from .model import (
    Context,
    Decision,
    Grant,
    Origin,
    ParamSpec,
    Policy,
    Scope,
    Taint,
    Tier,
    ToolCall,
    ToolMeta,
    ToolPolicy,
    Verdict,
)
from .paths import check_path
from .rate import check_rate

SELF_APPROVAL_RE = re.compile(r"(?i)^_*(confirm|approv|consent|authori[sz]|yes$|force$)")  # REQ-SEC-06
NO_LEADING_DASH_FORMATS = frozenset({"url", "host", "email", "phone"})  # REQ-SEC-56
BOX_SESSION_KINDS = frozenset({"code", "box"})  # REQ-SEC-59: sessions whose caller is in a box
ESCALATING_TIERS = frozenset({Tier.ACT, Tier.SEND, Tier.SYSTEM})  # REQ-SEC-33
URL_FORMATS = frozenset({"url", "host"})
_HOST_FORBIDDEN = frozenset("/@?#\\ ")  # a bare host argument holds a name and an optional port only
_URL_REASONS = {
    "url:scheme": "the URL scheme is not allowed (https only unless [egress].allow_http).",
    "url:userinfo": "URLs with a user name or password are not allowed.",
    "url:host": "the URL has no usable host.",
    "url:ip-literal": "IP address hosts are not allowed unless [egress].allow_ip_literals.",
    "url:private": "the host is a local or private name.",
}
_PATH_REASONS = {
    "path:workspace": "the path is inside an active session workspace, which host tools never touch.",
    "path:hard-deny": "the path is a credential or system location that is always denied.",
    "path:denied": "the path is denied by [paths].deny in policy.toml.",
    "path:hal-dir": "the path is inside Hal's own state, config or runtime directories.",
    "path:outside-roots": "the path is outside the roots this tool may use.",
}


@dataclass(frozen=True)
class ArgError:
    """Why validation failed: the `bad-args:<code>` suffix and a message for the caller."""

    code: str
    message: str


def strip_reserved(args: Mapping[str, object], known: frozenset[str]) -> tuple[dict, tuple, tuple]:
    """`(kept args, stripped "_" keys, approval-like keys)` for one call (REQ-SEC-06)."""
    kept = {k: v for k, v in args.items() if not str(k).startswith("_")}
    stripped = tuple(sorted(str(k) for k in args if str(k).startswith("_")))
    suspicious = tuple(
        sorted(
            k for k in map(str, args) if (k.startswith("_") or k not in known) and SELF_APPROVAL_RE.match(k)
        )
    )
    return kept, stripped, suspicious


def validate(meta: ToolMeta, args: Mapping[str, object]) -> ArgError | None:
    """The first problem with `args` against `meta.params`, or None (REQ-SEC-05)."""
    by_name = {p.name: p for p in meta.params}
    for name in args:
        if name not in by_name:
            accepted = ", ".join(sorted(by_name)) or "none"
            return ArgError("unknown", f"{meta.name}: unknown argument {name!r}; accepted: {accepted}.")
    for p in meta.params:
        if p.name not in args:
            if p.required:
                return ArgError("missing", f"{meta.name}: {p.name!r} is required.")
            continue
        value = args[p.name]
        if not _type_ok(value, p.type):
            return ArgError("type", f"{meta.name}: {p.name!r} must be {p.type}, got {type(value).__name__}.")
        if p.enum is not None and value not in p.enum:
            return ArgError("enum", f"{meta.name}: {p.name!r} must be one of {', '.join(map(str, p.enum))}.")
        if isinstance(value, str) and (problem := _string_problem(p, value)):
            return ArgError(problem[0], f"{meta.name}: {p.name!r} {problem[1]}.")
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
        return "command", "may not contain a newline or carriage return"
    if p.format in NO_LEADING_DASH_FORMATS and value.startswith("-"):
        return "leading-dash", "may not start with '-'"
    return None


def check_url_arg(value: str, fmt: str, tp: ToolPolicy, policy: Policy) -> tuple[str | None, bool]:
    """`(deny rule, host listed)` for one `url` or `host` argument under REQ-SEC-10 (no DNS)."""
    if fmt == "host":
        if not value or any(c in value for c in _HOST_FORBIDDEN):
            return "url:host", False
        value = f"https://{value}/"
    egress = policy.egress
    return netguard.check_url(
        value,
        domains=(*tp.domains, *egress.domains),
        allow_http=egress.allow_http,
        allow_ip_literals=egress.allow_ip_literals,
        allow_private=egress.allow_private,
    )


def from_search(url: str, policy: Policy, ctx: Context) -> bool:
    """True when `url` is a search result of this session and its host is not excluded (REQ-SEC-92)."""
    try:
        canonical = netguard.canonical_url(url)
    except ValueError:
        return False
    if canonical not in ctx.search_urls:
        return False
    # check_url's "listed" flag is reused as the exclusion match, so `*.host` entries work the
    # same way as in [egress].domains; the URL already passed REQ-SEC-10 with the real settings
    _, excluded = netguard.check_url(
        url,
        domains=policy.egress.from_search_exclude,
        allow_http=True,
        allow_ip_literals=True,
        allow_private=True,
    )
    return not excluded


def args_match(args: Mapping[str, object], allow_args: Mapping[str, tuple[str, ...]]) -> bool:
    """True when every `allow_args` parameter is present and matches one of its values or globs."""
    for name, patterns in allow_args.items():
        if name not in args:
            return False
        text = str(args[name]).lower() if isinstance(args[name], bool) else str(args[name])
        if not any(fnmatch.fnmatchcase(text, pattern) for pattern in patterns):
            return False
    return True


def match_grant(
    grants: tuple[Grant, ...], key: str, meta: ToolMeta, call: ToolCall, ctx: Context
) -> Grant | None:
    """The first live grant that covers this exact call (REQ-SEC-20, REQ-SEC-21), or None."""
    digest = security_digest(meta)
    for g in grants:
        if g.expires <= ctx.now:
            continue
        if g.scope in (Scope.ONCE, Scope.SESSION):
            if g.scope is Scope.ONCE and g.used:
                continue
            if (g.key, g.tool, g.pack, g.session, g.meta_sha256) == (
                key,
                meta.name,
                meta.pack,
                call.session,
                digest,
            ):
                return g
        elif (
            g.scope is Scope.ROUTINE
            and call.origin is Origin.ROUTINE
            and g.routine_id is not None
            and g.routine_id == call.routine_id
            and g.steps_sha256 is not None
            and g.steps_sha256 == ctx.routine_steps_sha256
            and g.step_keys.get(key) == digest
        ):
            return g
    return None


class _Builder:
    """Collects the trace and the fields every outcome after validation shares."""

    def __init__(self) -> None:
        self.trace: list[str] = []
        self.common: dict[str, object] = {"key": None, "args": {}, "stripped": (), "self_approval": ()}

    def step(self, rule: str) -> None:
        self.trace.append(rule)

    def done(self, verdict: Verdict, rule: str, reason: str, **extra: object) -> Decision:
        if not self.trace or self.trace[-1] != rule:
            self.trace.append(rule)
        return Decision(verdict, rule, reason, **{**self.common, **extra}, trace=tuple(self.trace))


def decide(call: ToolCall, meta: ToolMeta | None, policy: Policy, ctx: Context) -> Decision:
    """The policy's pure decision for one call; the first rule that returns wins."""
    b = _Builder()
    if meta is None:
        return b.done(Verdict.DENY, "unknown-tool", f"No tool named {call.tool}.")
    tp = policy.tool(meta)
    b.step(f"resolve:{tp.rule}")
    if not tp.enabled:
        return b.done(Verdict.DENY, "disabled", f"{meta.name} is disabled in policy.")
    args, stripped, suspicious = strip_reserved(call.args, frozenset(p.name for p in meta.params))
    b.common.update(stripped=stripped, self_approval=suspicious)
    if stripped:
        b.step("strip:" + ",".join(stripped))
    err = validate(meta, args)
    if err:
        return b.done(Verdict.DENY, f"bad-args:{err.code}", err.message)
    for p in meta.params:
        if p.format == "path" and p.name in args:
            rp = ctx.paths.get(p.name)
            if rp is None or rp.resolved is None:
                why = rp.error if rp is not None and rp.error else "unresolved"
                return b.done(Verdict.DENY, f"path:{why}", f"{p.name}: the path is {why.replace('_', ' ')}.")
            args[p.name] = str(rp.resolved)
    key = approval_key(meta.pack, meta.name, args)
    b.common.update(key=key, args=args)
    if tp.verdict is Verdict.DENY:
        return b.done(
            Verdict.DENY, tp.rule, f"{meta.name} is denied ({tp.rule}) in profile {policy.profile.name}."
        )
    if meta.runs_code and ctx.session_kind in BOX_SESSION_KINDS:
        return b.done(
            Verdict.DENY, "runs-code@box", f"{meta.name} runs code on the host and is never offered to a box."
        )
    for p in meta.params:
        if p.format == "path" and p.name in args:
            rule = check_path(ctx.paths[p.name], p.access, tp, policy, ctx)
            b.step(f"path:{p.name}")
            if rule:
                return b.done(Verdict.DENY, rule, f"{p.name}: {_PATH_REASONS[rule]}")
    return _verdict(call, meta, policy, ctx, tp, key, args, b)


def _verdict(
    call: ToolCall,
    meta: ToolMeta,
    policy: Policy,
    ctx: Context,
    tp: ToolPolicy,
    key: str,
    args: Mapping[str, object],
    b: _Builder,
) -> Decision:
    host_listed: bool | None = None
    url_values: list[str] = []
    for p in meta.params:
        if p.format in URL_FORMATS and isinstance(args.get(p.name), str):
            problem, listed = check_url_arg(args[p.name], p.format, tp, policy)
            b.step(f"url:{p.name}")
            if problem:
                return b.done(Verdict.DENY, problem, f"{p.name}: {_URL_REASONS[problem]}")
            host_listed = listed if host_listed is None else (host_listed and listed)
            # a bare host argument is never a search result, so it keeps url:from-search off
            url_values.append(args[p.name] if p.format == "url" else "")
    limited = check_rate(meta, tp, policy, ctx.rate)
    b.step("rate")
    if limited:
        return b.done(
            Verdict.DENY, limited, f"{meta.name}: the {limited[5:]} rate limit for this session is used up."
        )
    verdict, rule = tp.verdict, tp.rule
    # REQ-SEC-08: allow_args never promotes a system or runs_code tool, even if check_rules was skipped
    promotable = meta.tier is not Tier.SYSTEM and not meta.runs_code
    if verdict is Verdict.ASK and promotable and tp.allow_args and args_match(args, tp.allow_args):
        verdict, rule = Verdict.ALLOW, "allow-args"
        b.step(rule)
    if host_listed is True and verdict is Verdict.ASK and meta.tier is not Tier.SYSTEM:
        verdict, rule = Verdict.ALLOW, "domain-allowlist"
        b.step(rule)
    if host_listed is False and policy.egress.unlisted == "deny":
        return b.done(Verdict.DENY, "url:unlisted", "The host is not on an allowed domain list.")
    if host_listed is False and verdict is Verdict.ALLOW:
        verdict, rule = Verdict.ASK, "url:unlisted"
        b.step(rule)
    if (
        verdict is Verdict.ASK
        and meta.tier is not Tier.SYSTEM
        and meta.name in policy.egress.from_search
        and url_values
        and all(from_search(u, policy, ctx) for u in url_values)
    ):
        verdict, rule = Verdict.ALLOW, "url:from-search"
        b.step(rule)
    fresh_only = False
    if ctx.taint is Taint.FLAGGED and meta.tier in ESCALATING_TIERS:
        if policy.guard.on_flag == "deny":
            return b.done(
                Verdict.DENY, "flagged-turn", "This turn read flagged content, so this call is denied."
            )
        verdict = Verdict.ASK if verdict is Verdict.ALLOW else verdict
        rule, fresh_only = "flagged-turn", True
        b.step(rule)
    if verdict is Verdict.ALLOW:
        return b.done(Verdict.ALLOW, rule, f"{meta.name} is allowed ({rule}).")
    if not fresh_only:
        grant = match_grant(ctx.grants, key, meta, call, ctx)
        if grant is not None:
            return b.done(
                Verdict.ALLOW,
                f"grant:{grant.id}",
                f"{meta.name} is covered by grant {grant.id}.",
                grant_id=grant.id,
            )
        b.step("grants:none")
    if ctx.rate.counts.get("asks", 0) >= policy.rate.asks.count:
        return b.done(
            Verdict.DENY,
            "rate:asks",
            "Too many approval prompts in this session; stop and summarise for the user.",
        )
    if not ctx.approver_available:
        return b.done(
            Verdict.DENY, "no-approver", f"{meta.name} needs approval and no approval surface is attached."
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
    return b.done(
        Verdict.ASK,
        rule,
        f"{meta.name} needs approval ({rule}).",
        reviewable=reviewable,
        scopes=policy.allowed_scopes(meta),
        fresh_only=fresh_only,
    )


def explain(decision: Decision) -> str:
    """A readable account of `decision` for `hal policy explain`: verdict, rule, reason and trace."""
    lines = [f"{decision.verdict.value}  ({decision.rule})", f"  {decision.reason}"]
    if decision.stripped:
        lines.append(f"  stripped: {', '.join(decision.stripped)}")
    if decision.self_approval:
        lines.append(f"  self-approval attempt: {', '.join(decision.self_approval)}")
    if decision.key:
        lines.append(f"  key: {decision.key}")
    if decision.scopes:
        lines.append(f"  scopes: {', '.join(s.value for s in decision.scopes)}")
    lines.append("  trace:")
    lines.extend(f"    {i}. {step}" for i, step in enumerate(decision.trace, 1))
    return "\n".join(lines)

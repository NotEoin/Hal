"""The hub facade: one call pipeline that every front end runs (04-hub.md, "One call, step by step").

`Hub.call(name, args, session, front_end=...)` resolves the tool, normalises its arguments,
asks 01-security's `prepare` and `decide`, gets an approval when the verdict is `ask`, runs
the tool through the runner, scrubs and caps the result, applies taint and the guard, and
writes every audit record through the session's `EventSink`. The broker, `hal-hub mcp`, the
CLI and `halhub.client` all call this one method with their own `HubSession` (D20).

Design decisions:

- Fail closed everywhere: an exception in `prepare`, `decide` or the context builder is a
  deny with rule `internal-error` (REQ-HUB-19); an `ask` runs only after a matching grant or
  an approver `allow` whose scope the request offered (REQ-HUB-20); a pre-execution audit
  record that cannot be written refuses the call (REQ-HUB-51).
- The tool runs with exactly `Decision.args`; `call_arguments` builds a new mapping for the
  function and nothing writes to the decision's arguments (01 REQ-SEC-18).
- The hub adds approval grants and writes `approval_request` and `approval` itself, so every
  approver, router or bare surface, is audited the same way (see `halhub.approvals.router`).
- A module's secrets are read as soon as the tool is known, before the `decision` record, so
  a secret value the model echoes in an argument is redacted from every record of the call;
  a secret that cannot be read fails the call only at the run step, as `tool_error`.
- A `once` grant, whether from an approver, the reviewer or an earlier answer, is consumed
  under the grant store's lock right before the run, so two concurrent calls cannot share it.
- A routine-defining tool's steps are parsed, normalised and decided before any prompt (a bad
  step makes the save `invalid_arguments`), shown step by step in the request, and a human
  `allow` adds one 01 `routine` grant whose `step_keys` are the steps that would ask, bound
  to the routine id and the step list's hash (REQ-HUB-67). 01's grant model holds every
  asking step in one grant, so the hub adds one grant per save, not one per step.
- Results of `untrusted_output` tools come back inside 01's untrusted-content envelope, after
  secret scrubbing and output capping, so the guard scans exactly what the caller receives
  (01 REQ-SEC-34, REQ-SEC-96).
- The policy, approvals, audit, envelope and guard client are 01's modules. With
  `[guard] enabled = true` and no scanner passed in, each scan goes through a
  `halhub.guard.GuardClient` for the session's `[guard]` settings; a relay that is not
  running makes every scan an error, which flags the session (fail closed, REQ-SEC-35). The
  reviewer is `halhub.reviewer`; none is built by default (see that module's header).
- After a result of a tool in `[egress].search_tools`, the session records the result's
  URLs for `url:from-search` (01 REQ-SEC-92). Rate counts are recorded per session: a prompt
  when an `ask` reaches the approver, a call when it ran (REQ-SEC-12).
- Listings (`find`, `function_specs`, `tools`, `get`, `describe`, `modules`) leave out every
  tool whose listing verdict, `session.policy.tool(meta)` without arguments, is `deny` or
  disabled (REQ-HUB-23). Argument-dependent rules (paths, domains) cannot hide a tool; they
  can only deny a call.
- A hub that loaded its own registry reloads it on a miss (an unknown tool name), at most once
  per `REFRESH_MIN_INTERVAL`, so a long-lived process sees a newly installed pack without
  rescanning on every typo. A registry passed in by the caller is never replaced.
- The built-in `hub` module's tools run in-process with the hub and session bound by
  `halhub.builtin.bound`, so they answer for the calling session.
- Provider tools (host MCP servers, `providers.py`) are looked up after the registry and run
  through the same steps; only the run itself goes to the provider.
- A worker's nested `tool_call` runs through this same pipeline with the parent's session and
  no approver: it must be in the calling module's `calls`, get a plain `allow` (no grant), stay
  one level deep, and finish within the parent's remaining time (REQ-HUB-31, D15). The calling
  module is the one the runner knows owns the worker, never a name the worker sends.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from halkit import paths
from halkit.canon import canonical_json
from halkit.termsafe import escape
from halkit.timeutil import iso_z, utc_now

from . import approvals, builtin, policy, search
from .approvals.keys import security_digest
from .args import ArgumentError, call_arguments, normalise
from .audit import AuditLog, AuditUnavailable
from .classifier import ChatFn, Classifier, ClassifyRequest
from .config import HubConfig
from .guard import GuardClient, GuardResult, GuardScanner
from .manifest import ModuleSpec, ToolSpec
from .policy.rate import RateLimiter
from .providers import (
    PROVIDER_NAME_RE,
    PROVIDER_PACK_PREFIX,
    PROVIDER_TOOL_RE,
    TOOL_SEPARATOR,
    ToolProvider,
    provider_meta,
)
from .registry import Registry
from .reviewer import CallReviewer, ReviewRequest
from .runner import NestedCall, Runner, RunOutcome, SecretError, cut_output, resolve_secrets, scrub
from .session import EventSink, HubSession, SessionKind, default_workspaces, new_session_id, parse_origin
from .untrusted import GuardLabel, wrap

log = logging.getLogger(__name__)

FrontEnd = Literal["python", "cli", "mcp", "broker", "nested"]
FRONT_ENDS: tuple[str, ...] = get_args(FrontEnd)
CALL_ID_BYTES = 8  # "c_" + 16 hex (01 ToolCall.call_id)
ROUTINE_GRANTS = "routine-grants.jsonl"  # under state_dir(), 01 "Grant store"
REVIEW_REASON_CHARS = 300  # 01 audit schema: the reviewer's reason, cut
REFRESH_MIN_INTERVAL = 30.0  # seconds between registry reloads on a miss (04-hub "Hub facade")
MAX_ALWAYS_OFFER = 8  # 04-hub "Search": fixed tools stay a small share of a chat model's context
FIND_DESCRIPTION_CHARS = 300  # 04-hub "MCP server": hal_find_tools cuts each description

__all__ = ["CallResult", "FrontEnd", "Hub", "RoutineRun", "RoutineStep", "new_call_id", "steps_sha256"]


def new_call_id() -> str:
    """`c_` + 16 hex."""
    return "c_" + secrets.token_hex(CALL_ID_BYTES)


@dataclass(frozen=True)
class CallResult:
    """What one call produced, for every front end."""

    status: Literal["ok", "error", "denied"]
    call_id: str
    tool: str
    result: object = None
    error_type: str | None = None
    rule: str | None = None  # 01 rule id for denials, e.g. "no-approver"
    message: str | None = None
    warnings: tuple[str, ...] = ()
    untrusted: bool = False
    guard: GuardLabel = "off"
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """The dict form: `status`, then `result` or `error_type`, `rule`, `message` and v1's `error`."""
        if self.status == "ok":
            return {"status": "ok", "result": self.result, "warnings": list(self.warnings)}
        out: dict[str, object] = {"status": self.status, "error_type": self.error_type}
        if self.rule:
            out["rule"] = self.rule
        out["message"] = self.message
        out["error"] = self.message
        return out


@dataclass(frozen=True)
class RoutineRun:
    """A routine step run by the routines daemon: the routine id and its stored step list."""

    routine_id: str
    steps: str  # the canonical JSON step list the routine was saved with


@dataclass(frozen=True)
class RoutineStep:
    """One step of a routine being saved, normalised and decided as it would run unattended."""

    tool: str
    tier: str
    args: Mapping[str, object]  # normalised, before path resolution: what the step list stores
    verdict: str
    rule: str
    key: str | None
    meta_sha256: str


def steps_sha256(steps: Sequence[RoutineStep]) -> str:
    """sha256 hex of `canonical_json([{"tool", "args"}, ...])` over the normalised steps (01 "Routines")."""
    doc = [{"tool": step.tool, "args": dict(step.args)} for step in steps]
    return hashlib.sha256(canonical_json(doc)).hexdigest()


@dataclass(frozen=True)
class _Target:
    """A resolved tool with its module (or provider), policy metadata and the module's secrets."""

    tool: ToolSpec
    module: ModuleSpec | None  # None for a provider tool
    meta: policy.ToolMeta
    secrets: Mapping[str, str]
    secret_error: str | None  # why the secrets could not be read; the run step reports it
    provider: ToolProvider | None = None


class Hub:
    """The registry, runner and call pipeline behind every front end."""

    def __init__(
        self,
        registry: Registry | None = None,
        *,
        runner: Runner | None = None,
        guard: GuardScanner | None = None,
        reviewer: CallReviewer | None = None,
        classifier: Classifier | None = None,
        paranoia_floor: str | None = None,
        classify_chat: ChatFn | None = None,
        config: HubConfig | None = None,
        policy_path: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        home: Path | None = None,
    ) -> None:
        self.config = config if config is not None else HubConfig.load()
        self._owns_registry = registry is None
        self.registry = registry if registry is not None else Registry.load(self.config)
        self._registry_loaded = time.monotonic()
        self._refresh_lock = threading.Lock()
        self.runner = runner if runner is not None else Runner(self.config.runner)
        self.guard = guard
        self.reviewer = reviewer
        self.classifier = classifier
        self.paranoia_floor = paranoia_floor
        self.classify_chat = classify_chat
        self.grants = approvals.GrantStore(paths.state_dir() / ROUTINE_GRANTS, clock=clock)
        self._policy_path = policy_path
        self._clock = clock
        self._home = home if home is not None else Path.home()
        self._secret_values: set[str] = set()
        self._providers: dict[str, ToolProvider] = {}
        self._provider_tools: dict[str, tuple[ToolSpec, ToolProvider]] = {}
        self._secret_lock = threading.Lock()

    # -- sessions --

    def open_session(
        self,
        *,
        kind: SessionKind,
        origin: str,
        channel: str | None = None,
        profile: str | None = None,
        approver: approvals.Approver | None = None,
        audit: EventSink | None = None,
        session_id: str | None = None,
        remote_addr: str | None = None,
        resumed: bool = False,
    ) -> HubSession:
        """A session with this hub's defaults; raises `halkit.config.ConfigError` for a bad policy file.

        `resumed` marks a session reopened after a restart; it starts `untrusted` (01 F5).
        """
        loaded = policy.load_policy(self._policy_path, profile=profile, channel=channel, home=self._home)
        if audit is None:
            audit = AuditLog(settings=loaded.audit, secrets=self.known_secrets, clock=self._clock)
        return HubSession(
            id=session_id or new_session_id(),
            kind=kind,
            origin=origin,
            channel=channel,
            profile=loaded.profile.name,
            policy=loaded,
            approver=approver,
            grants=self.grants,
            rate=RateLimiter(clock=time.monotonic),
            audit=audit,
            workspaces=default_workspaces(),
            home=self._home,
            remote_addr=remote_addr,
            resumed=resumed,
        )

    def known_secrets(self) -> tuple[str, ...]:
        """Every secret value this hub has injected so far, for audit redaction."""
        with self._secret_lock:
            return tuple(self._secret_values)

    def add_provider(self, provider: ToolProvider) -> None:
        """Adds a provider's tools under pack `mcp-<name>`; raises `ValueError` on any name clash.

        The hub sets each tool's tier from `provider.default_tier`, `untrusted_output` true and
        `reviewable` false, whatever the provider returned.
        """
        name = provider.name
        if not PROVIDER_NAME_RE.fullmatch(name):
            raise ValueError(f"provider name {name!r} must match {PROVIDER_NAME_RE.pattern}")
        if name in self._providers:
            raise ValueError(f"a provider named {name!r} is already added")
        added: dict[str, tuple[ToolSpec, ToolProvider]] = {}
        for spec in provider.tools():
            if not spec.name.startswith(name + TOOL_SEPARATOR) or not PROVIDER_TOOL_RE.fullmatch(spec.name):
                raise ValueError(
                    f"provider {name} offers {spec.name!r}; its tools must be named {name}__<tool>"
                )
            if spec.name in added or spec.name in self._provider_tools or self.registry.lookup(spec.name):
                raise ValueError(f"provider {name} offers {spec.name!r}, which another tool already uses")
            added[spec.name] = (
                dataclasses.replace(
                    spec,
                    module=name,
                    pack=PROVIDER_PACK_PREFIX + name,
                    tier=str(provider.default_tier),
                    tier_inferred=False,
                    untrusted_output=True,
                    reviewable=False,
                ),
                provider,
            )
        self._providers[name] = provider
        self._provider_tools.update(added)

    def provider_tools(self) -> list[ToolSpec]:
        """Every provider tool, sorted by name."""
        return sorted((spec for spec, _ in self._provider_tools.values()), key=lambda t: t.name)

    def close(self) -> None:
        """Stops every worker and every provider."""
        self.runner.close()
        for provider in self._providers.values():
            try:
                provider.close()
            except Exception:  # one provider failing to stop must not leave the others running
                log.exception("provider %s did not close cleanly", provider.name)

    # -- listings --

    def refresh(self, *, force: bool = False) -> bool:
        """Reloads the registry if this hub loaded it and `REFRESH_MIN_INTERVAL` has passed (or
        `force`); True if it did. Raises `RegistryError` when the new scan cannot be built."""
        if not self._owns_registry:
            return False
        with self._refresh_lock:
            if not force and time.monotonic() - self._registry_loaded < REFRESH_MIN_INTERVAL:
                return False
            self.registry = Registry.load(self.config)
            self._registry_loaded = time.monotonic()
        log.info("reloaded the registry after a miss")
        return True

    def _meta(self, tool: ToolSpec) -> policy.ToolMeta:
        if tool.name in self._provider_tools:
            return provider_meta(tool)
        return tool.meta(self.registry.module_of(tool))

    def tool_meta(self, tool: ToolSpec) -> policy.ToolMeta:
        """01's `ToolMeta` for a registry or provider tool: what `decide` and the listings read."""
        return self._meta(tool)

    def listed(self, tool: ToolSpec, session: HubSession) -> policy.ToolPolicy | None:
        """The tool's listing policy for `session`, or None when the session may not see it."""
        resolved = session.policy.tool(self._meta(tool))
        if resolved.verdict is policy.Verdict.DENY or not resolved.enabled:
            return None
        return resolved

    def tools(self, session: HubSession) -> list[ToolSpec]:
        """Every tool of an `ok` module or a provider that `session` may use, sorted by name."""
        every = self.registry.tools() + self.provider_tools()
        return sorted((t for t in every if self.listed(t, session) is not None), key=lambda t: t.name)

    def get(self, name: str, session: HubSession) -> ToolSpec | None:
        """The tool called `name` if `session` may use it; a miss may reload the registry once."""
        found = self._usable(name, session)
        if found is None and self.refresh():
            found = self._usable(name, session)
        return found

    def _usable(self, name: str, session: HubSession) -> ToolSpec | None:
        tool = self.registry.tool(name)
        if tool is None and name in self._provider_tools:
            tool = self._provider_tools[name][0]
        return tool if tool is not None and self.listed(tool, session) is not None else None

    def find(self, query: str, limit: int = 8, *, session: HubSession) -> list[ToolSpec]:
        """Up to `limit` tools that `session` may use, best match for `query` first (04-hub "Search")."""
        keywords = {name: state.spec.keywords for name, state in self.registry.modules.items()}
        return search.rank(query, self.tools(session), limit, module_keywords=keywords)

    def function_specs(self, query: str, limit: int = 8, *, session: HubSession) -> list[dict[str, object]]:
        """OpenAI-style function definitions: `always_offer` tools, `[hub].pinned`, then search hits.

        At most `MAX_ALWAYS_OFFER` `always_offer` tools are offered, sorted by name; more are
        left out with a warning. No tool appears twice (REQ-HUB-35).
        """
        usable = {t.name: t for t in self.tools(session)}
        always = [t for t in usable.values() if t.always_offer]
        if len(always) > MAX_ALWAYS_OFFER:
            dropped = ", ".join(t.name for t in always[MAX_ALWAYS_OFFER:])
            log.warning("more than %d always_offer tools; left out: %s", MAX_ALWAYS_OFFER, dropped)
            always = always[:MAX_ALWAYS_OFFER]
        pinned = [usable[name] for name in self.config.pinned if name in usable]
        picked: dict[str, ToolSpec] = {}
        for tool in always + pinned + self.find(query, limit, session=session):
            picked.setdefault(tool.name, tool)
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.input_schema()},
            }
            for t in picked.values()
        ]

    def describe(self, tool: ToolSpec, session: HubSession) -> dict[str, object]:
        """What `hal_find_tools` returns for one tool (04-hub "MCP server"); `approval` is `ask` or `none`."""
        resolved = self.listed(tool, session)
        return {
            "name": tool.name,
            "module": tool.module,
            "pack": tool.pack,
            "description": tool.description[:FIND_DESCRIPTION_CHARS],
            "tier": tool.tier,
            "approval": "ask" if resolved is not None and resolved.verdict is policy.Verdict.ASK else "none",
            "parameters": tool.input_schema(),
        }

    def modules(self, session: HubSession) -> list[dict[str, object]]:
        """Each registry module with its pack, summary, status and the tools `session` may use."""
        usable = {t.name for t in self.tools(session)}
        return [
            {
                "module": name,
                "pack": state.spec.pack,
                "summary": state.spec.summary,
                "status": state.status,
                "tools": [t.name for t in state.spec.tools if t.name in usable],
            }
            for name, state in self.registry.modules.items()
        ]

    # -- the pipeline --

    def call(
        self,
        name: str,
        args: Mapping[str, object] | None,
        session: HubSession,
        *,
        front_end: FrontEnd,
        cancel: threading.Event | None = None,
        routine: RoutineRun | None = None,
        timeout: float | None = None,
    ) -> CallResult:
        """Runs one call through every pipeline step; never raises for a tool or policy problem.

        `routine` is set only by the routines daemon's client calls (origin `routine`): the hub
        recomputes the stored step list's hash so `decide` can match the routine grant.
        `timeout` caps the tool's own timeout (`halhub.client.call`, and the parent's remaining
        time for a nested call).
        `front_end="nested"` has no approver, so an `ask` is denied, and a grant does not count
        either: a nested call runs only on a plain `allow` (REQ-HUB-31, D15).
        """
        if front_end not in FRONT_ENDS:
            raise ValueError(f"unknown front end {front_end!r}")
        started = time.monotonic()
        call_id = new_call_id()

        def finish(status: str, **fields: object) -> CallResult:
            """The `CallResult` with this call's id, tool name and elapsed time."""
            return CallResult(status, call_id, name, seconds=time.monotonic() - started, **fields)

        found = self.registry.lookup(name)
        if found is None and name not in self._provider_tools and self.refresh():
            found = self.registry.lookup(name)
        target = None
        if found is not None:
            tool, state = found
            module = state.spec
            target = self._target(tool, module)
            if state.status != "ok":
                reason = f"{name} is unavailable: {state.reason or state.status}"
                return self._refuse_early(
                    session, call_id, target, "unavailable", reason, finish, "unavailable"
                )
        elif name in self._provider_tools:
            spec, provider = self._provider_tools[name]
            target = _Target(spec, None, provider_meta(spec), {}, None, provider)

        warnings: tuple[str, ...] = ()
        steps: tuple[RoutineStep, ...] = ()
        if target is not None:
            try:
                normalised = normalise(target.tool, args, near_miss=front_end == "cli")
            except ArgumentError as exc:
                return self._refuse_early(
                    session, call_id, target, f"bad-args:{exc.code}", str(exc), finish, "invalid_arguments"
                )
            raw, warnings = normalised.args, normalised.warnings
            if target.tool.routine is not None:
                try:
                    steps = self._expand_steps(target.tool, raw, session)
                except ArgumentError as exc:
                    return self._refuse_early(
                        session,
                        call_id,
                        target,
                        "bad-args:routine-step",
                        str(exc),
                        finish,
                        "invalid_arguments",
                    )
        else:
            raw = dict(args) if isinstance(args, Mapping) else {}

        origin, remote_client = parse_origin(session.origin)
        call = policy.ToolCall(
            tool=name,
            args=raw,
            session=session.id,
            origin=origin,
            call_id=call_id,
            channel=session.channel,
            remote_client=remote_client,
            remote_addr=session.remote_addr,
            routine_id=routine.routine_id if routine else None,
        )
        meta = target.meta if target else None
        failed = False
        try:
            resolved = policy.prepare(call, meta, home=self._home) if meta else {}
            run_sha = self._run_sha256(routine, session) if routine else None
            ctx = session.context(
                self._clock(),
                resolved,
                routine_steps_sha256=run_sha,
                approver=front_end != "nested",
                meta=meta,
            )
            decision = policy.decide(call, meta, session.policy, ctx)
        except Exception:  # REQ-HUB-19: any policy failure is a deny, never a run
            log.exception("policy failed for %s; denying the call", name)
            failed = True
            decision = policy.Decision(
                policy.Verdict.DENY,
                "internal-error",
                "The policy engine failed; the call was denied.",
                None,
                {},
                (),
                (),
            )

        try:
            self._audit_decision(session, call, meta, decision)
            if decision.self_approval:
                session.emit(
                    "self_approval_attempt", call=call_id, tool=name, keys=list(decision.self_approval)
                )
        except AuditUnavailable as exc:
            return finish("error", error_type="audit_unavailable", message=str(exc))

        if failed:
            return finish("denied", error_type="policy_error", rule=decision.rule, message=decision.reason)
        if decision.verdict is policy.Verdict.DENY:
            return finish(**_deny_fields(name, target, decision))
        assert target is not None  # decide denies an unknown tool
        if front_end == "nested" and decision.grant_id is not None:
            return finish(
                **_denied(
                    "nested:grant", f"{name} is covered only by a grant; a nested call needs a plain allow."
                )
            )
        classifier = self._classifier_for(session)
        if classifier is not None and classifier.paranoia != "off":
            decision, refusal = self._classify(classifier, target, call, decision, session)
            if refusal is not None:
                return finish(**refusal)
        if decision.verdict is policy.Verdict.ASK:
            session.rate.record_ask(session.id)
            refusal = self._approve(target, call, decision, session, steps)
            if refusal is not None:
                return finish(**refusal)
        elif decision.grant_id is not None and not session.grants.consume(decision.grant_id):
            return finish(
                "denied",
                error_type="policy_denied",
                rule="grant-used",
                message=f"grant {decision.grant_id} was already used",
            )
        limit = target.tool.timeout if timeout is None else min(target.tool.timeout, timeout)
        nested = front_end == "nested"
        return self._execute(target, decision, session, call_id, cancel, warnings, finish, limit, nested)

    def approval_key(self, name: str, args: Mapping[str, object] | None, session: HubSession) -> str | None:
        """The approval key `call` would bind for these arguments now, or None when there is none.

        Repeats steps 1 to 5 (resolve, normalise, prepare, decide) without audit records and
        without running anything, so an MCP retry that carries an approval answer can be
        checked against the request it answers (04-hub "Approvals over MCP"). Any failure
        gives None, which matches no request.
        """
        found = self.registry.lookup(name)
        if found is not None:
            target = self._target(found[0], found[1].spec)
        elif name in self._provider_tools:
            spec, provider = self._provider_tools[name]
            target = _Target(spec, None, provider_meta(spec), {}, None, provider)
        else:
            return None
        try:
            raw = normalise(target.tool, args).args
            origin, remote_client = parse_origin(session.origin)
            call = policy.ToolCall(
                tool=name,
                args=raw,
                session=session.id,
                origin=origin,
                call_id=new_call_id(),
                channel=session.channel,
                remote_client=remote_client,
                remote_addr=session.remote_addr,
            )
            resolved = policy.prepare(call, target.meta, home=self._home)
            ctx = session.context(self._clock(), resolved, approver=True, meta=target.meta)
            return policy.decide(call, target.meta, session.policy, ctx).key
        except Exception:  # a retry whose key cannot be computed matches nothing: fail closed
            log.warning("could not recompute the approval key for %s", name, exc_info=True)
            return None

    # -- pipeline steps --

    def _target(self, tool: ToolSpec, module: ModuleSpec) -> _Target:
        """Resolves the module's secrets first, so every audit record of the call can redact them."""
        try:
            values = resolve_secrets(module)
        except SecretError as exc:
            return _Target(tool, module, tool.meta(module), {}, str(exc))
        with self._secret_lock:
            self._secret_values.update(v for v in values.values() if v)
        return _Target(tool, module, tool.meta(module), values, None)

    def _nested_handler(
        self, session: HubSession, *, deadline: float, depth_ok: bool
    ) -> Callable[[NestedCall], RunOutcome]:
        """Answers a worker's `tool_call`: declared in `calls`, one level deep, parent's time left."""

        def handle(request: NestedCall) -> RunOutcome:
            if not depth_ok:
                return self._refuse_nested(
                    session, request, "nested:depth", "a nested call cannot make another"
                )
            if request.tool not in request.module.calls:
                reason = f"{request.module.name} does not list {request.tool} in its calls"
                return self._refuse_nested(session, request, "nested:undeclared", reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return RunOutcome(
                    False, error_type="timeout", message=f"{request.parent_tool.name} ran out of time"
                )
            r = self.call(request.tool, request.args, session, front_end="nested", timeout=remaining)
            return RunOutcome(r.status == "ok", r.result, r.error_type, r.message, rule=r.rule)

        return handle

    def _refuse_nested(self, session: HubSession, request: NestedCall, rule: str, reason: str) -> RunOutcome:
        """Audits a nested call refused before `decide` and returns the refusal for the worker."""
        found = self.registry.lookup(request.tool)
        meta = found[0].meta(found[1].spec) if found else None
        try:
            self._emit_refusal(session, new_call_id(), request.tool, meta, rule, reason)
        except AuditUnavailable as exc:
            return RunOutcome(False, error_type="audit_unavailable", message=str(exc))
        return RunOutcome(False, error_type="policy_denied", message=reason, rule=rule)

    def _emit_refusal(
        self,
        session: HubSession,
        call_id: str,
        name: str,
        meta: policy.ToolMeta | None,
        rule: str,
        reason: str,
    ) -> None:
        session.emit(
            "decision",
            call=call_id,
            pack=meta.pack if meta else None,
            pack_version=meta.pack_version if meta else None,
            tool=name,
            tier=str(meta.tier) if meta else None,
            key=None,
            verdict="deny",
            rule=rule,
            reason=reason,
            stripped=[],
            taint=session.current_taint().label,
            grant=None,
        )

    def _refuse_early(
        self,
        session: HubSession,
        call_id: str,
        target: _Target,
        rule: str,
        reason: str,
        finish: Callable[..., CallResult],
        error_type: str,
    ) -> CallResult:
        """Audits and returns a refusal found before `decide` (unavailable module, bad arguments)."""
        try:
            self._emit_refusal(session, call_id, target.meta.name, target.meta, rule, reason)
        except AuditUnavailable as exc:
            return finish("error", error_type="audit_unavailable", message=str(exc))
        return finish("error", error_type=error_type, rule=rule, message=reason)

    def _audit_decision(
        self, session: HubSession, call: policy.ToolCall, meta: policy.ToolMeta | None, d: policy.Decision
    ) -> None:
        fields: dict[str, object] = {
            "call": call.call_id,
            "pack": meta.pack if meta else None,
            "pack_version": meta.pack_version if meta else None,
            "tool": call.tool,
            "tier": str(meta.tier) if meta else None,
            "key": d.key,
            "verdict": str(d.verdict),
            "rule": d.rule,
            "reason": d.reason,
            "stripped": list(d.stripped),
            "taint": session.current_taint().label,
            "grant": d.grant_id,
        }
        if session.policy.audit.args:
            fields["args"] = _audit_args(meta, d.args, content=session.policy.audit.content)
        session.emit("decision", **fields)

    def _approve(
        self,
        target: _Target,
        call: policy.ToolCall,
        d: policy.Decision,
        session: HubSession,
        steps: tuple[RoutineStep, ...] = (),
    ) -> dict[str, object] | None:
        """Step 7: the reviewer or the session's approver; returns None when approved, else the refusal."""
        meta = target.meta
        if d.reviewable and self.reviewer is not None and session.last_human_message:
            if self._review(target, call, d, session):
                grant = self._grant(meta, d, session, policy.Scope.ONCE, source="reviewer")
                session.grants.add(grant, settings=session.policy.approval)
                session.grants.consume(grant.id)
                return None
        approver = session.approver
        if approver is None or not approver.available():
            return _denied("no-approver", f"{meta.name} needs approval and no approval surface is available.")
        request = self._request(meta, call, d, session, steps)
        surfaces = [s.name for s in getattr(approver, "surfaces", (approver,))]
        try:
            session.emit(
                "approval_request",
                call=call.call_id,
                approval=request.id,
                key=request.key,
                surfaces=surfaces,
                scopes=[str(s) for s in request.scopes],
                expires_at=request.expires_at,
            )
        except AuditUnavailable as exc:
            return {"status": "error", "error_type": "audit_unavailable", "message": str(exc)}
        asked = time.monotonic()
        timeout_s = session.policy.approval.timeout_s
        try:
            answer = approver.decide(request, timeout_s)
        except Exception:  # a broken approver counts as a deny, never as an allow
            log.exception("approver %s failed", approver.name)
            answer = approvals.Decision(request.id, request.key, "deny", None, "error")
        ms = int((time.monotonic() - asked) * 1000)
        if answer.request_id != request.id or answer.key != request.key:
            try:
                session.emit(
                    "approval_mismatch",
                    approval=request.id,
                    key_expected=request.key,
                    key_got=answer.key,
                    source=approver.name,
                )
            except AuditUnavailable as exc:
                log.error("approval_mismatch record failed: %s", exc)
            return _denied("approval-mismatch", "The approval answer did not match this exact call.")
        grant = routine_grant = None
        if answer.verdict == "allow" and answer.scope in request.scopes:
            grant = self._grant(meta, d, session, answer.scope, source=answer.source)
            routine_grant = self._routine_grant(target, d, session, steps, source=answer.source)
        try:
            session.emit(
                "approval",
                call=call.call_id,
                approval=request.id,
                key=request.key,
                verdict=answer.verdict,
                scope=str(answer.scope) if answer.scope else None,
                source=answer.source,
                grant=grant.id if grant else None,
                routine_grant=routine_grant.id if routine_grant else None,
                ms=ms,
            )
        except AuditUnavailable as exc:
            return {"status": "error", "error_type": "audit_unavailable", "message": str(exc)}
        if grant is not None:
            if routine_grant is not None:
                try:
                    session.grants.add(
                        routine_grant,
                        steps=[_step_view(step) for step in steps],
                        settings=session.policy.approval,
                    )
                except OSError as exc:  # the save must not run when its grant was not stored
                    log.error("routine grant for %s could not be stored: %s", call.call_id, exc)
                    return {
                        "status": "error",
                        "error_type": "tool_error",
                        "message": f"cannot store grant: {exc}",
                    }
            session.grants.add(grant, settings=session.policy.approval)
            if not session.grants.consume(grant.id):
                return _denied("grant-used", f"grant {grant.id} expired before the call started")
            return None
        if answer.verdict == "allow":
            return _denied(
                "approval-scope", f"The answer chose scope {answer.scope}, which this request did not offer."
            )
        if answer.source == "timeout":
            return _denied(
                "approval-timeout",
                f"{meta.name} needed the user's approval and none came within {timeout_s} s. "
                "Ask the user to approve it, then call it again with the same arguments.",
            )
        if answer.source == "no-approver":
            return _denied("no-approver", f"{meta.name} needs approval and no approval surface is available.")
        note = f" The user said: {answer.note}" if answer.note else ""
        return {
            "status": "denied",
            "error_type": "approval_declined",
            "rule": "approval-declined",
            "message": f"The user declined {meta.name}.{note}",
        }

    def _classifier_for(self, session: HubSession) -> Classifier | None:
        """The classifier for `session`: the one given, else one built from its `[policy]`.

        A hub started with a `paranoia_floor` (a boxed session; the broker sets it from the
        lockdown level) always classifies, even with no model: that counts as `high` risk.
        """
        if self.classifier is not None:
            return self.classifier
        if self.paranoia_floor is None:
            return None
        return Classifier(session.policy.classifier, self.classify_chat, floor=self.paranoia_floor)

    def _classify(
        self,
        classifier: Classifier,
        target: _Target,
        call: policy.ToolCall,
        d: policy.Decision,
        session: HubSession,
    ) -> tuple[policy.Decision, dict[str, object] | None]:
        """Rates the call and applies the paranoia table: `(decision, None)` or `(d, refusal)`.

        The classifier only tightens: an allow may become an ask (a `once` approval, no grant
        consumed) or a deny; an ask may become a deny; and the reviewer is kept for the cells of
        the table that allow it.
        """
        tool_policy = session.policy.tool(target.meta)
        roots = tuple(str(Path(r).expanduser()) for r in (*tool_policy.read_roots, *tool_policy.write_roots))
        request = ClassifyRequest(
            target.meta,
            d.args,
            session.current_taint(),
            session.last_human_message,
            target.tool.description,
            roots,
        )
        verdict = classifier.classify(request)
        outcome = classifier.apply(d.verdict, verdict)
        try:
            session.emit(
                "classification",
                call=call.call_id,
                paranoia=classifier.paranoia,
                risk=verdict.risk,
                reason=verdict.reason,
                rules=list(verdict.rules),
                model=verdict.model,
                ms=verdict.ms,
                error=verdict.error,
                verdict=str(outcome.verdict),
                changed=outcome.changed,
            )
        except AuditUnavailable as exc:
            return d, {"status": "error", "error_type": "audit_unavailable", "message": str(exc)}
        if outcome.verdict is policy.Verdict.DENY and d.verdict is not policy.Verdict.DENY:
            return d, _denied(outcome.rule, f"{target.meta.name} was refused: {outcome.reason}")
        if outcome.changed:  # allow -> ask: a fresh once-approval, never a stored grant
            d = dataclasses.replace(
                d,
                verdict=policy.Verdict.ASK,
                rule=outcome.rule,
                reason=outcome.reason,
                scopes=(policy.Scope.ONCE,),
                grant_id=None,
            )
        return dataclasses.replace(d, reviewable=d.reviewable and outcome.review_ok), None

    def _review(
        self, target: _Target, call: policy.ToolCall, d: policy.Decision, session: HubSession
    ) -> bool:
        """Asks the reviewer; True only for `approve`. A reviewer failure escalates to the human."""
        assert self.reviewer is not None
        request = ReviewRequest(
            session.last_human_message,
            target.meta,
            d.args,
            session.current_taint(),
            description=target.tool.description,
        )
        error = None
        try:
            result = self.reviewer.review(request)
            decision, reason, ms, model = result.decision, result.reason, result.ms, result.model
        except Exception as exc:  # the reviewer never denies and never fails a call: escalate
            log.warning("reviewer failed for %s: %s", call.tool, exc)
            decision, reason, ms, model, error = "escalate", "", 0, session.policy.reviewer.model, str(exc)
        try:
            session.emit(
                "review",
                call=call.call_id,
                model=model,
                decision=decision,
                reason=reason[:REVIEW_REASON_CHARS],
                ms=ms,
                error=error,
            )
        except AuditUnavailable as exc:
            log.error("review record failed, escalating to the human: %s", exc)
            return False
        return decision == "approve"

    def _request(
        self,
        meta: policy.ToolMeta,
        call: policy.ToolCall,
        d: policy.Decision,
        session: HubSession,
        steps: tuple[RoutineStep, ...] = (),
    ) -> approvals.ApprovalRequest:
        now = self._clock()
        timeout_s = session.policy.approval.timeout_s
        content = {p.name for p in meta.params if p.content}
        lines = [f"{meta.name}  [{meta.tier}]  {meta.pack}"]
        for key in sorted(d.args):
            value = d.args[key]
            text = value if isinstance(value, str) else json.dumps(value)
            lines.append(f"  {key}: {escape(text, multiline=key in content)}")
        for number, step in enumerate(steps, 1):  # 01 "Routines" step 1: render every step
            canonical = canonical_json(dict(step.args)).decode("ascii")
            lines.append(f"  step {number}: {step.tool}  [{step.tier}]  {step.verdict}")
            lines.append(f"    {escape(canonical, multiline=False)}")
        assert d.key is not None  # an ask always carries the key of its validated arguments
        return approvals.ApprovalRequest(
            id=approvals.new_request_id(),
            session=session.id,
            origin=call.origin,
            channel=session.channel,
            pack=meta.pack,
            pack_version=meta.pack_version,
            tool=meta.name,
            tier=meta.tier,
            key=d.key,
            reason=f"{d.rule} - {d.reason}",
            args=dict(d.args),
            display="\n".join(lines),
            summary=f"Allow {meta.name} ({meta.tier}, pack {meta.pack})?",
            content=bool(content),
            challenge=None,
            scopes=d.scopes,
            taint=session.current_taint(),
            guard=session.guard_label,
            created_at=iso_z(now, ms=True),
            expires_at=iso_z(now + timedelta(seconds=timeout_s), ms=True),
        )

    def _expand_steps(
        self, tool: ToolSpec, args: Mapping[str, object], session: HubSession
    ) -> tuple[RoutineStep, ...]:
        """Step 8, before any prompt: parse, normalise and decide every step; raises `ArgumentError`."""
        assert tool.routine is not None
        text = args.get(tool.routine.steps_param)
        if not isinstance(text, str):
            return ()  # a missing or mistyped steps argument is decide's bad-args to report
        return self._parse_steps(tool.name, text, session)

    def _parse_steps(self, owner: str, text: str, session: HubSession) -> tuple[RoutineStep, ...]:
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ArgumentError(owner, None, f"steps are not JSON: {exc}", code="routine-step") from None
        items = [raw] if isinstance(raw, dict) else raw
        if not isinstance(items, list) or not items:
            raise ArgumentError(
                owner, None, "steps must be a non-empty list of {tool, args}", code="routine-step"
            )
        steps = []
        for number, item in enumerate(items, 1):
            where = f"step {number}"
            if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
                raise ArgumentError(owner, None, f"{where} needs a tool name", code="routine-step")
            if set(item) - {"tool", "args"} or not isinstance(item.get("args", {}), dict):
                raise ArgumentError(
                    owner, None, f"{where} must be {{tool, args}} with object args", code="routine-step"
                )
            name = item["tool"]
            step_tool = self.registry.tool(name)
            if step_tool is None:
                raise ArgumentError(
                    owner, None, f"{where} names no available tool {name!r}", code="routine-step"
                )
            try:
                normalised = normalise(step_tool, item.get("args", {})).args
            except ArgumentError as exc:
                raise ArgumentError(owner, None, f"{where}: {exc}", code="routine-step") from None
            steps.append(self._decide_step(owner, where, step_tool, normalised, session))
        return tuple(steps)

    def _decide_step(
        self, owner: str, where: str, tool: ToolSpec, args: Mapping[str, object], session: HubSession
    ) -> RoutineStep:
        """The step's verdict as it would run unattended: origin `routine`, no grants, clean taint."""
        meta = tool.meta(self.registry.module_of(tool))
        call = policy.ToolCall(tool.name, args, session.id, policy.Origin.ROUTINE, new_call_id())
        resolved = policy.prepare(call, meta, home=self._home)
        ctx = policy.Context(
            now=self._clock(),
            session_kind="routine",
            taint=policy.Taint.CLEAN,
            grants=(),
            rate=policy.RateSnapshot(),
            approver_available=True,
            paths=resolved,
            workspaces=session.workspaces(),
            home=self._home,
        )
        d = policy.decide(call, meta, session.policy, ctx)
        if d.rule.startswith("bad-args:"):
            raise ArgumentError(owner, None, f"{where}: {d.reason}", code="routine-step")
        return RoutineStep(
            tool.name, str(meta.tier), dict(args), str(d.verdict), d.rule, d.key, security_digest(meta)
        )

    def _run_sha256(self, routine: RoutineRun, session: HubSession) -> str | None:
        """The stored step list's hash, recomputed; None when it no longer parses or validates."""
        try:
            return steps_sha256(self._parse_steps("routine", routine.steps, session))
        except ArgumentError as exc:
            log.warning("routine %s: stored steps no longer validate: %s", routine.routine_id, exc)
            return None

    def _routine_grant(
        self,
        target: _Target,
        d: policy.Decision,
        session: HubSession,
        steps: tuple[RoutineStep, ...],
        *,
        source: str,
    ) -> policy.Grant | None:
        """01's `routine` grant for the steps that would ask, bound to the routine id and step hash."""
        routine = target.tool.routine
        asking = {s.key: s.meta_sha256 for s in steps if s.verdict == "ask" and s.key}
        if routine is None or not asking or source == "reviewer":
            return None
        return approvals.routine_grant(
            routine_id=str(d.args[routine.id_param]),
            steps_sha256=steps_sha256(steps),
            step_keys=asking,
            settings=session.policy.approval,
            now=self._clock(),
            source=source,
        )

    def _grant(
        self,
        meta: policy.ToolMeta,
        d: policy.Decision,
        session: HubSession,
        scope: policy.Scope,
        *,
        source: str,
    ) -> policy.Grant:
        make = approvals.once_grant if scope is policy.Scope.ONCE else approvals.session_grant
        assert d.key is not None  # an ask always carries the key of its validated arguments
        return make(
            key=d.key,
            meta=meta,
            session=session.id,
            settings=session.policy.approval,
            now=self._clock(),
            source=source,
        )

    def _execute(
        self,
        target: _Target,
        d: policy.Decision,
        session: HubSession,
        call_id: str,
        cancel: threading.Event | None,
        warnings: tuple[str, ...],
        finish: Callable[..., CallResult],
        timeout: float,
        nested: bool,
    ) -> CallResult:
        """Steps 9 to 12: run, scrub and cap, taint and guard, audit the result."""
        tool, module = target.tool, target.module
        started = time.monotonic()
        handler = self._nested_handler(session, deadline=started + timeout, depth_ok=not nested)
        secret_values = dict(target.secrets)
        if target.provider is not None:
            outcome = _provider_call(target.provider, tool, call_arguments(tool, d.args), timeout, cancel)
        elif target.secret_error is not None:
            outcome = RunOutcome(False, error_type="tool_error", message=target.secret_error)
        else:
            assert module is not None  # only provider tools have no module
            with builtin.bound(self, session):
                outcome = self.runner.run(
                    module,
                    tool,
                    call_arguments(tool, d.args),
                    timeout=timeout,
                    secrets=secret_values,
                    cancel=cancel,
                    on_tool_call=handler,
                )
        ms = int((time.monotonic() - started) * 1000)
        guard: GuardLabel = "off"
        if outcome.ok:
            result, text = _shape(scrub(outcome.result, secret_values), tool.max_output)
            if tool.name in session.policy.egress.search_tools:
                session.search_urls.record(session.id, text)
            if tool.untrusted_output:
                session.raise_taint(policy.Taint.UNTRUSTED, cause="untrusted_output")
                guard, score = self._scan(session, call_id, tool, text)
                result = text = wrap(text, tool=tool.name, guard=guard, score=score)
            self._audit_result(session, call_id, tool, True, None, text, ms)
            session.rate.record_call(session.id, target.meta)
            return finish(
                "ok", result=result, warnings=warnings, untrusted=tool.untrusted_output, guard=guard
            )
        message = scrub(outcome.message or "", secret_values)
        self._audit_result(session, call_id, tool, False, outcome.error_type, "", ms)
        session.rate.record_call(session.id, target.meta)
        return finish(
            "error", error_type=outcome.error_type or "tool_error", message=message, warnings=warnings
        )

    def _scan(
        self, session: HubSession, call_id: str, tool: ToolSpec, text: str
    ) -> tuple[GuardLabel, float | None]:
        """Step 11: the guard scan when `[guard]` is on; any failure flags (REQ-SEC-35)."""
        settings = session.policy.guard
        if not settings.enabled:
            return "off", None
        scanner = self.guard if self.guard is not None else GuardClient(settings)
        try:
            result = scanner.scan(text, source_tool=tool.name)
        except Exception as exc:  # fail closed: a guard failure counts as a flag
            result = GuardResult("error", None, 0, 0, settings.model, error=str(exc))
        label: GuardLabel = {"benign": "clean", "injection": "flagged"}.get(result.label, "error")
        session.guard_label = label
        if label != "clean":
            session.raise_taint(policy.Taint.FLAGGED, cause="guard")
        try:
            session.emit(
                "guard_scan",
                call=call_id,
                tool=tool.name,
                model=result.model,
                label=result.label,
                score=result.score,
                windows=result.windows,
                chars=len(text),
                ms=result.ms,
                error=result.error,
            )
        except AuditUnavailable as exc:
            log.error("guard_scan record failed after the tool ran: %s", exc)
        return label, result.score

    def _audit_result(
        self,
        session: HubSession,
        call_id: str,
        tool: ToolSpec,
        ok: bool,
        error_type: str | None,
        text: str,
        ms: int,
    ) -> None:
        fields: dict[str, object] = {
            "call": call_id,
            "tool": tool.name,
            "ok": ok,
            "error_type": error_type,
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "ms": ms,
        }
        if session.policy.audit.content:
            fields["content"] = text
        try:
            session.emit("result", **fields)
        except AuditUnavailable as exc:  # REQ-HUB-51: the tool ran; return its result, the gap shows in seq
            log.error("result record for %s failed: %s", call_id, exc)


def _provider_call(
    provider: ToolProvider,
    tool: ToolSpec,
    args: Mapping[str, object],
    timeout: float,
    cancel: threading.Event | None,
) -> RunOutcome:
    """Runs a provider tool; a provider that raises gives `tool_error`, never a traceback to the caller."""
    try:
        return provider.call(tool.function, args, timeout=timeout, cancel=cancel)
    except Exception as exc:  # 02 owns the upstream; its failures are this call's error
        log.exception("provider %s failed on %s", provider.name, tool.name)
        return RunOutcome(False, error_type="tool_error", message=f"{provider.name} failed: {exc}")


def _step_view(step: RoutineStep) -> dict[str, object]:
    """A step as stored beside its routine grant (01 `RoutineGrantView`)."""
    return {"tool": step.tool, "tier": step.tier, "args": dict(step.args), "verdict": step.verdict}


def _denied(rule: str, message: str) -> dict[str, object]:
    return {"status": "denied", "error_type": "policy_denied", "rule": rule, "message": message}


def _deny_fields(name: str, target: _Target | None, d: policy.Decision) -> dict[str, object]:
    """The `CallResult` fields of a `deny` decision (step 6)."""
    if target is None:
        message = f"no tool named {name}; use hal_find_tools to search"
        return {"status": "error", "error_type": "unknown_tool", "rule": d.rule, "message": message}
    if d.rule.startswith("bad-args:"):
        return {"status": "error", "error_type": "invalid_arguments", "rule": d.rule, "message": d.reason}
    return _denied(d.rule, d.reason)


def _audit_args(
    meta: policy.ToolMeta | None, args: Mapping[str, object], *, content: bool
) -> dict[str, object]:
    """Arguments for the audit record: `content = true` parameters as length and digest (01 REQ-SEC-41)."""
    hidden = {p.name for p in meta.params if p.content} if meta and not content else set()
    out: dict[str, object] = {}
    for key, value in args.items():
        if key in hidden:
            text = value if isinstance(value, str) else json.dumps(value)
            out[key] = {"chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}
        else:
            out[key] = value
    return out


def _shape(result: object, limit: int) -> tuple[object, str]:
    """The result to return and its text form, cut to `limit` characters (REQ-HUB-33)."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return result, text
    cut = cut_output(text, limit)
    return cut, cut

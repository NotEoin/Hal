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
  approver, router or bare surface, is audited the same way (see `_approvals_standin`).
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
- 01's modules are phase 2: until then the pipeline uses `_policy_standin`,
  `_approvals_standin`, `_audit_standin` and `_guard_standin`, whose headers list what each
  one leaves out. Nested calls (plan step 12), providers (step 13), search and listings
  (steps 14 and 15) join this facade in later steps.
"""

from __future__ import annotations

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
from halkit.timeutil import iso_z, utc_now

from . import _approvals_standin as approvals
from . import _policy_standin as policy
from ._audit_standin import AuditLog, AuditUnavailable
from ._guard_standin import GuardClient, GuardLabel, GuardResult, Reviewer, ReviewRequest, wrap
from .args import ArgumentError, call_arguments, normalise
from .config import HubConfig
from .manifest import ModuleSpec, ToolSpec
from .registry import Registry
from .runner import Runner, RunOutcome, SecretError, cut_output, resolve_secrets, scrub
from .session import EventSink, HubSession, SessionKind, default_workspaces, new_session_id, parse_origin

log = logging.getLogger(__name__)

FrontEnd = Literal["python", "cli", "mcp", "broker", "nested"]
FRONT_ENDS: tuple[str, ...] = get_args(FrontEnd)
CALL_ID_BYTES = 8  # "c_" + 16 hex (01 ToolCall.call_id)
ROUTINE_GRANTS = "routine-grants.jsonl"  # under state_dir(), 01 "Grant store"
REVIEW_REASON_CHARS = 300  # 01 audit schema: the reviewer's reason, cut

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
    return hashlib.sha256(policy.canonical_json(doc)).hexdigest()


@dataclass(frozen=True)
class _Target:
    """A resolved tool with its module, policy metadata and the module's secrets."""

    tool: ToolSpec
    module: ModuleSpec
    meta: policy.ToolMeta
    secrets: Mapping[str, str]
    secret_error: str | None  # why the secrets could not be read; the run step reports it


class Hub:
    """The registry, runner and call pipeline behind every front end."""

    def __init__(
        self,
        registry: Registry | None = None,
        *,
        runner: Runner | None = None,
        guard: GuardClient | None = None,
        reviewer: Reviewer | None = None,
        config: HubConfig | None = None,
        policy_path: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        home: Path | None = None,
    ) -> None:
        self.config = config if config is not None else HubConfig.load()
        self.registry = registry if registry is not None else Registry.load(self.config)
        self.runner = runner if runner is not None else Runner(self.config.runner)
        self.guard = guard
        self.reviewer = reviewer
        self.grants = approvals.GrantStore(paths.state_dir() / ROUTINE_GRANTS, clock=clock)
        self._policy_path = policy_path
        self._clock = clock
        self._home = home if home is not None else Path.home()
        self._secret_values: set[str] = set()
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
    ) -> HubSession:
        """A session with this hub's defaults; raises `halkit.config.ConfigError` for a bad policy file."""
        loaded = policy.load_policy(self._policy_path, profile=profile, channel=channel)
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
            rate=policy.RateLimiter(),
            audit=audit,
            workspaces=default_workspaces(),
            remote_addr=remote_addr,
        )

    def known_secrets(self) -> tuple[str, ...]:
        """Every secret value this hub has injected so far, for audit redaction."""
        with self._secret_lock:
            return tuple(self._secret_values)

    def close(self) -> None:
        """Stops every worker."""
        self.runner.close()

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
    ) -> CallResult:
        """Runs one call through every pipeline step; never raises for a tool or policy problem.

        `routine` is set only by the routines daemon's client calls (origin `routine`): the hub
        recomputes the stored step list's hash so `decide` can match the routine grant.
        """
        if front_end not in FRONT_ENDS:
            raise ValueError(f"unknown front end {front_end!r}")
        started = time.monotonic()
        call_id = new_call_id()

        def finish(status: str, **fields: object) -> CallResult:
            """The `CallResult` with this call's id, tool name and elapsed time."""
            return CallResult(status, call_id, name, seconds=time.monotonic() - started, **fields)

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
            ctx = session.context(self._clock(), resolved, routine_steps_sha256=run_sha)
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
        if decision.verdict is policy.Verdict.ASK:
            session.rate.record(meta, asked=True)
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
        return self._execute(target, decision, session, call_id, cancel, warnings, finish)

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
        meta = target.meta
        try:
            session.emit(
                "decision",
                call=call_id,
                pack=meta.pack,
                pack_version=meta.pack_version,
                tool=meta.name,
                tier=str(meta.tier),
                key=None,
                verdict="deny",
                rule=rule,
                reason=reason,
                stripped=[],
                taint=session.current_taint().label,
                grant=None,
            )
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
                session.grants.add(grant)
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
                    session.grants.add(routine_grant, steps=[_step_view(step) for step in steps])
                except OSError as exc:  # the save must not run when its grant was not stored
                    log.error("routine grant for %s could not be stored: %s", call.call_id, exc)
                    return {
                        "status": "error",
                        "error_type": "tool_error",
                        "message": f"cannot store grant: {exc}",
                    }
            session.grants.add(grant)
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

    def _review(
        self, target: _Target, call: policy.ToolCall, d: policy.Decision, session: HubSession
    ) -> bool:
        """Asks the reviewer; True only for `approve`. A reviewer failure escalates to the human."""
        assert self.reviewer is not None
        request = ReviewRequest(session.last_human_message, target.meta, d.args, session.current_taint())
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
            lines.append(f"  {key}: {approvals.escape(text, multiline=key in content)}")
        for number, step in enumerate(steps, 1):  # 01 "Routines" step 1: render every step
            canonical = policy.canonical_json(dict(step.args)).decode("ascii")
            lines.append(f"  step {number}: {step.tool}  [{step.tier}]  {step.verdict}")
            lines.append(f"    {approvals.escape(canonical, multiline=False)}")
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
            tool.name, str(meta.tier), dict(args), str(d.verdict), d.rule, d.key, policy.security_digest(meta)
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
        now = self._clock()
        return policy.Grant(
            id=approvals.new_grant_id(),
            scope=policy.Scope.ROUTINE,
            key=None,
            pack=None,
            tool=None,
            session=None,
            routine_id=str(d.args[routine.id_param]),
            steps_sha256=steps_sha256(steps),
            step_keys=asking,
            meta_sha256=None,
            created=now,
            expires=now + timedelta(days=session.policy.approval.routine_max_days),
            used=False,
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
        now = self._clock()
        settings = session.policy.approval
        ttl = (
            timedelta(seconds=settings.once_ttl_s)
            if scope is policy.Scope.ONCE
            else timedelta(hours=settings.session_max_h)
        )
        return policy.Grant(
            id=approvals.new_grant_id(),
            scope=scope,
            key=d.key,
            pack=meta.pack,
            tool=meta.name,
            session=session.id,
            routine_id=None,
            steps_sha256=None,
            step_keys={},
            meta_sha256=policy.security_digest(meta),
            created=now,
            expires=now + ttl,
            used=False,
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
    ) -> CallResult:
        """Steps 9 to 12: run, scrub and cap, taint and guard, audit the result."""
        tool, module = target.tool, target.module
        started = time.monotonic()
        secret_values = dict(target.secrets)
        if target.secret_error is not None:
            outcome = RunOutcome(False, error_type="tool_error", message=target.secret_error)
        else:
            outcome = self.runner.run(
                module,
                tool,
                call_arguments(tool, d.args),
                timeout=tool.timeout,
                secrets=secret_values,
                cancel=cancel,
            )
        ms = int((time.monotonic() - started) * 1000)
        guard: GuardLabel = "off"
        if outcome.ok:
            result, text = _shape(scrub(outcome.result, secret_values), tool.max_output)
            if tool.untrusted_output:
                session.raise_taint(policy.Taint.UNTRUSTED, cause="untrusted_output")
                guard, score = self._scan(session, call_id, tool, text)
                result = text = wrap(text, tool=tool.name, guard=guard, score=score)
            self._audit_result(session, call_id, tool, True, None, text, ms)
            session.rate.record(target.meta)
            return finish(
                "ok", result=result, warnings=warnings, untrusted=tool.untrusted_output, guard=guard
            )
        message = scrub(outcome.message or "", secret_values)
        self._audit_result(session, call_id, tool, False, outcome.error_type, "", ms)
        session.rate.record(target.meta)
        return finish(
            "error", error_type=outcome.error_type or "tool_error", message=message, warnings=warnings
        )

    def _scan(
        self, session: HubSession, call_id: str, tool: ToolSpec, text: str
    ) -> tuple[GuardLabel, float | None]:
        """Step 11: the guard scan when `[guard]` is on; a missing client or a failure flags (REQ-SEC-35)."""
        if not session.policy.guard.enabled:
            return "off", None
        if self.guard is None:
            result = GuardResult("error", None, 0, 0, "none", error="guard enabled but no guard client")
        else:
            try:
                result = self.guard.scan(text, source_tool=tool.name)
            except Exception as exc:  # fail closed: a guard failure counts as a flag
                result = GuardResult("error", None, 0, 0, "unknown", error=str(exc))
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

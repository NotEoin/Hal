"""The agent loop: model call, tool calls, repeat, bounded by a turn and a tool-call limit.

Tools come from the workspace and, when a hub socket is configured, from MCP; a built-in tool
wins a name clash. With `tool_format = "prompt"` the tools are described in the system prompt
and the model calls them with `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`
(for models without native tool calls). Tool errors go back to the model, never raise.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace

from halagent.architect import run_split
from halagent.assistant import AssistantOptions
from halagent.assistant.claims import ClaimsCheck
from halagent.assistant.prompts import PROFILE_CHARS, system_prompt
from halagent.backends import BrokerBackend, ToolBackend, ToolSpec
from halagent.budget import (
    FREE_TOOLS,
    Budget,
    BudgetClock,
    Watchdog,
    canonical,
    loop_message,
    nudge_text,
)
from halagent.constrain import request_keys, unwrap_answer
from halagent.context import (
    CHARS_PER_TOKEN,
    ContextManager,
    ContextOverflow,
    Fitted,
    StateNote,
    compaction_prompt,
    render_for_summary,
)
from halagent.hooks import Hooks
from halagent.images import extract_images
from halagent.mcp.client import UserMcp
from halagent.mcp.lite import McpError
from halagent.profiles import Profile, resolve_profile
from halagent.prompts import render_code_prompt
from halagent.provider import ChatProvider, Completion, ModelInfo, ProviderError, make_provider
from halagent.repair import has_open_marker, repair_call, repair_text
from halagent.repair.aliases import parse_args
from halagent.sessions import SessionWriter, replay_indexed, title_from_prompt
from halagent.settings import DEFAULT_MAX_OUTPUT, DEFAULT_WINDOW, AgentSettings
from halagent.subagents import DEFAULT_SLOTS, describe_agents, discover_agents, run_subagents
from halagent.tokens import ESTIMATE_FACTOR, TokenCounter
from halagent.tools import COMPACT_TOOLS, ToolError, Toolset, render_error
from halagent.tools.repomap import DEFAULT_MAP_TOKENS, build_map
from halkit import paths

log = logging.getLogger("halagent.loop")

GATED_KINDS = frozenset({"edit", "delete", "move", "execute"})  # review mode asks before these
ALLOW, REJECT, CANCELLED = ("allow_once", "allow_always"), ("reject_once", "reject_always"), "cancelled"
MAX_MODEL_NAME = 200  # model ids are short; a longer name is a mistake or an attack
PermissionFn = Callable[[str, str, str, dict], str]  # (call id, tool name, kind, arguments) -> choice

FIND_NAME = "hal_find_tools"
FIND_DEFAULT, FIND_MAX = 8, 20  # the broker's own defaults (04-hub)
MAX_OFFERED_FOUND = 16  # REQ-AGT-52: at most 16 host tools offered at once
META_NAMES = frozenset({"hal_find_tools", "hal_call"})
FIND_PROPS = {
    "query": {"type": "string", "description": "what you want to do, in plain words"},
    "limit": {"type": "integer", "description": "most tools to return (default 8)"},
}
FIND_SPEC = {
    "type": "function",
    "function": {
        "name": FIND_NAME,
        "description": "Search host tools for what the user asked; found tools become callable.",
        "parameters": {"type": "object", "properties": FIND_PROPS, "required": ["query"]},
    },
}
MAX_OUTPUT_SHARE = 4  # a reply may take at most a quarter of the window
OVERFLOW_SHRINK = 0.75  # after a context_length_exceeded answer, plan for 25% less input
FOCUS_MAX_CHARS = 500  # a focus hint longer than this would crowd the small model's summary prompt
HEAD_SHARE = 3  # a cut result keeps the first third and the last two thirds of the cap


def _describe(fn: dict) -> str:
    """`name(arg: type, ...): description` for the prompt-format tool block."""
    props = fn["parameters"].get("properties", {})
    args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
    return f"{fn['name']}({args}): {fn['description']}"


def _error(error_type: str, message: str) -> str:
    """The JSON error envelope for a call that did not run."""
    return json.dumps({"status": "error", "error_type": error_type, "message": message})


@dataclass
class RunResult:
    """Outcome of one prompt: final text, why it stopped, and counts."""

    text: str
    stopped: str  # end reason: done, finished, max_tool_calls, max_model_requests, max_time, loop, ...
    turns: int
    tool_calls: int


def _as_mcp(spec: ToolSpec) -> dict:
    return {"name": spec.name, "description": spec.description, "inputSchema": spec.parameters}


def effective_settings(settings: AgentSettings, profile: Profile, info: ModelInfo) -> AgentSettings:
    """Fills the limits the user left at their defaults from the gateway, then the profile.

    Precedence per key: an explicit setting, the gateway (window only), the profile, the default.
    """
    window = settings.context_window
    if window == DEFAULT_WINDOW:
        window = info.context_length or int(profile.values.get("context", window))
    out = settings.max_output
    if out == DEFAULT_MAX_OUTPUT:
        out = int(profile.values.get("max_output", out))
    out = min(out, max(1, window // MAX_OUTPUT_SHARE))
    fmt = settings.tool_format
    if fmt == "native" and profile.values.get("tool_format") == "prompt":
        fmt = "prompt"
    return replace(settings, context_window=window, max_output=out, tool_format=fmt)


def _now() -> float:
    """Monotonic seconds; tests replace it so tokens per second is deterministic."""
    return time.monotonic()


TITLE_PROMPT = "Write a title of at most six words for this coding request. Reply with the title only."
TITLE_MAX_CHARS = 60


class Agent:
    """A conversation with tools. `run` may be called repeatedly; history persists."""

    def __init__(
        self,
        settings: AgentSettings,
        toolset: Toolset,
        provider: ChatProvider | None = None,
        on_event: Callable[[str, str], None] | None = None,
        session: SessionWriter | None = None,
        system_extra: str = "",
        on_update: Callable[[dict], None] | None = None,
        profile: Profile | None = None,
        permission: PermissionFn | None = None,
        assistant: AssistantOptions | None = None,
        user_mcp: UserMcp | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self.hooks = hooks or None
        self._constraint = "none"
        self._fast: ChatProvider | None = None
        self._main_model = settings.models.get("main") or settings.model
        self.split = False  # True: each turn runs architect then editor (see `halagent.architect`)
        if self.hooks:
            self.hooks.lifecycle("session_start")
        self.assistant = assistant
        self.user_mcp = (
            None if assistant else user_mcp
        )  # an assistant box starts no user server (REQ-AGT-102)
        self.claims = (
            ClaimsCheck(has_tools=bool(settings.broker)) if assistant and assistant.claims_check else None
        )
        self.mode = "auto"  # "review": gated tools call `permission` first; "plan": gated tools are refused
        self.permission = permission
        self._remembered: dict[str, str] = {}  # tool name -> allow_always or reject_always
        built = provider is None
        self._requested = settings  # what the person asked for, before the model's own limits
        self._built = built
        self.provider = provider or make_provider(settings)
        info = self.provider.model_info() if hasattr(self.provider, "model_info") else ModelInfo()
        self.profile = profile or resolve_profile(
            settings.model, user_file=paths.config_dir() / "model-profiles.toml", hal_profile=info.hal_profile
        )
        if built:
            self.provider.extra = self.profile.request_extras()
        self.settings = effective_settings(settings, self.profile, info)
        if built and hasattr(self.provider, "max_output"):
            self.provider.max_output = self.settings.max_output
        settings = self.settings
        self.system_extra = system_extra
        if settings.repo_map_auto and not assistant:
            # built once, here: the prompt is the cached prefix and must not change between turns
            listing = build_map(toolset.workspace.root, "", DEFAULT_MAP_TOKENS)
            self.system_extra = f"{system_extra}\n\nRepository map:\n{listing}".strip()
        self.on_update = on_update or (lambda update: None)
        self.cancel = threading.Event()
        self.toolset = toolset
        self.on_event = on_event or (lambda kind, text: None)
        self.backend: ToolBackend | None = None
        self._mcp_tools: dict[str, dict] = {}  # offered host tools: name -> MCP-style tool
        self._hidden: dict[str, ToolSpec] = {}  # listed but kept behind `hal_find_tools`
        self._profile_text = ""
        if settings.broker:
            self.backend = BrokerBackend(settings.broker)
            listed = self.backend.list_tools()
            self._offer_listed(listed)  # the assistant offers none up front: tools are chosen per turn
            if assistant and any(t.name == "memory_profile" for t in listed):
                self._profile_text = self._fetch_profile()
        if self.profile.values.get("tool_set") == "compact":
            keep = set(COMPACT_TOOLS)
            self.toolset.tools = {n: t for n, t in self.toolset.tools.items() if n in keep}
        if "subagent" in self.toolset.tools:
            sub = self.toolset.tools["subagent"]
            described = sub.description + describe_agents(discover_agents(self.toolset.workspace.root))
            self.toolset.tools["subagent"] = replace(
                sub, fn=lambda a: run_subagents(self, a), description=described
            )
        self.model_slots = info.slots or DEFAULT_SLOTS
        self.session = session
        self.tokens = TokenCounter("openai", settings.context_window)
        self.counters: Counter[str] = Counter()  # per-run quality counters for the eval (REQ-AGT-95)
        self.turn = 0
        self._next_i = 0
        self.note = StateNote()
        t = settings.tuning
        self.context = ContextManager(
            settings.context_window, settings.max_output, self._summarize, keep_recent=t.keep_recent,
            mask_at=t.mask_at, mask_to=t.mask_to, summarize_at=t.summarize_at, safety_tokens=t.safety_tokens,
        )  # fmt: skip
        self._ids: list[int] = []
        self._focus = ""
        self.messages: list[dict] = []
        self._add({"role": "system", "content": self._system_prompt()})

    def _add(self, msg: dict, **extra: object) -> None:
        """Appends a message to the history and, with a session, to the transcript."""
        self.messages.append(msg)
        self._ids.append(self._next_i)
        if self.session:
            rec = {"type": "message", "turn": self.turn, "i": self._next_i, "msg": msg, **extra}
            self.session.write(rec)
        self._next_i += 1

    def _summarize(self, old: list[dict]) -> str:
        """One model call, no tools, that compresses `old` into the compaction headings."""
        focus = f"\n\nPay special attention to: {self._focus}" if self._focus else ""
        request = [
            {"role": "system", "content": compaction_prompt() + focus},
            {"role": "user", "content": render_for_summary(old)},
        ]
        return self._fast_provider().complete(request, None).text

    def _fit(self) -> list[dict]:
        """The history to send; compacts it (and records that) when it is over budget."""
        tools_chars = len(json.dumps(self._specs())) if self.settings.tool_format == "native" else 0
        return self._apply_fitted(self.context.fit(self.messages, self._ids, self.note, tools_chars))

    def compact(self, focus: str = "") -> tuple[int, int]:
        """Summarises the whole history now; returns the estimated tokens before and after.

        `focus` is optional text (the `/compact` argument) that the summary should keep detail on.
        """
        before = self.context.estimate(self.messages)
        self._focus = focus.strip()[:FOCUS_MAX_CHARS]
        try:
            fitted = self.context.compact_all(self.messages, self._ids, self.note)
        finally:
            self._focus = ""
        if not fitted.records:
            raise ValueError("there is nothing to compact yet")
        self._apply_fitted(fitted, announce=False)
        after = self.context.estimate(self.messages)
        self.on_update(
            {"kind": "compaction", "reason": "manual", "before_tokens": before, "after_tokens": after}
        )
        return before, after

    def _apply_fitted(self, fitted: Fitted, announce: bool = True) -> list[dict]:
        """Adopts a fitted history and writes its compaction records to the transcript."""
        self.messages, self._ids = fitted.messages, fitted.ids
        for rec in fitted.records:
            if rec["kind"] in ("summary", "note"):
                self.counters["compactions"] += 1
            if rec["kind"] in ("summary", "note"):
                rec = {**rec, "summary_i": self._next_i}
            if announce and rec["kind"] in ("summary", "note", "mask"):
                self.on_update(
                    {
                        "kind": "compaction",
                        "reason": "auto",
                        "method": rec["kind"],
                        "before_tokens": rec["before_tokens"],
                        "after_tokens": rec["after_tokens"],
                    }
                )
            if self.session:
                self.session.write({**rec, "turn": self.turn, "id": f"cmp_{self._next_i}"})
        if fitted.summary_at is not None:
            self._ids[fitted.summary_at] = self._next_i
            if self.session:
                msg = self.messages[fitted.summary_at]
                self.session.write({"type": "message", "turn": self.turn, "i": self._next_i, "msg": msg})
            self._next_i += 1
        return self.messages

    @property
    def accepts_images(self) -> bool:
        """True when the provider speaks chat completions and the profile does not turn vision off."""
        return getattr(self.provider, "route", "") == "/v1/chat/completions" and bool(
            self.profile.values.get("vision", True)
        )

    @staticmethod
    def _with_image_parts(m: dict) -> dict:
        """Turns a user message's `images` into chat-completions content parts."""
        images = m.get("images")
        if not images:
            return m
        parts: list[dict] = [{"type": "text", "text": str(m.get("content") or "")}]
        parts += [
            {"type": "image_url", "image_url": {"url": f"data:{i['mimeType']};base64,{i['data']}"}}
            for i in images
        ]
        return {**{k: v for k, v in m.items() if k != "images"}, "content": parts}

    def _wire(self, history: list[dict]) -> list[dict]:
        """Applies the profile's `system_role` and `strict_alternation` to the outgoing copy."""
        history = [self._with_image_parts(m) for m in history]
        role = str(self.profile.values.get("system_role", "system"))
        out = [{**m, "role": role} if m is history[0] and role != "system" else m for m in history]
        if self.profile.values.get("strict_alternation"):
            merged: list[dict] = []
            for m in out:
                prev = merged[-1] if merged else None
                if (
                    prev
                    and m["role"] == prev["role"] == "user"
                    and "tool_calls" not in m
                    and isinstance(m["content"], str)
                    and isinstance(prev["content"], str)
                ):
                    merged[-1] = {**prev, "content": f"{prev['content']}\n\n{m['content']}"}
                else:
                    merged.append(m)
            out = merged
        return out

    def resume(self, records: list[dict]) -> None:
        """Continues a stored session: replaces the history with the replayed messages."""
        pairs = replay_indexed(records)
        if not pairs or pairs[0][1].get("role") != "system":
            raise ValueError("the stored session has no system message")
        replayed = [m for _, m in pairs]
        replayed[0] = self.messages[0]  # the current prompt wins over the stored one
        self.messages, self._ids = replayed, [i for i, _ in pairs]
        self._next_i = 1 + max((r["i"] for r in records if r["type"] == "message"), default=-1)
        self.turn = max((r.get("turn", 0) for r in records if r["type"] == "message"), default=0)

    def _offer_listed(self, listed: list[ToolSpec]) -> None:
        """Offers the broker's listed tools up to the profile's `max_tools_hint`; the rest wait."""
        room = max(0, int(self.profile.values.get("max_tools_hint", 12)) - len(self.toolset.tools) - 1)
        if self.assistant:
            room = 0
        for spec in listed:
            if spec.name in self.toolset.tools:
                continue
            if len(self._mcp_tools) < room:
                self._mcp_tools[spec.name] = _as_mcp(spec)
            else:
                self._hidden[spec.name] = spec

    def _fetch_profile(self) -> str:
        """The user's memory profile through the broker, cut to `PROFILE_CHARS`; empty when unavailable."""
        assert self.backend is not None
        out = self.backend.call("memory_profile", {})
        return out.text[:PROFILE_CHARS] if out.status == "ok" else ""

    def _select_for_turn(self, user_text: str) -> None:
        """Offers the broker's best `tools_per_turn` tools for this user message (REQ-AGT-52)."""
        assert self.assistant and self.backend
        for name in list(self._mcp_tools):
            self._hidden[name] = ToolSpec(
                name, self._mcp_tools[name]["description"], self._mcp_tools[name]["inputSchema"]
            )
        self._mcp_tools.clear()
        for spec in self.backend.find_tools(user_text, self.assistant.tools_per_turn)[
            : self.assistant.tools_per_turn
        ]:
            if spec.name not in META_NAMES:
                self._mcp_tools[spec.name] = _as_mcp(spec)
                self._hidden.pop(spec.name, None)

    def _find_tools(self, args: dict) -> str:
        """The agent's `hal_find_tools`: asks the broker, ranks hidden tools, offers what it found."""
        assert self.backend is not None  # only offered with a backend
        query = str(args.get("query", "")).strip()
        limit = min(max(int(args.get("limit") or FIND_DEFAULT), 1), FIND_MAX)
        found = {t.name: t for t in self._rank_hidden(query, limit)}
        for spec in self.backend.find_tools(query, limit):
            found.setdefault(spec.name, spec)
        rows = []
        for name, spec in found.items():
            if name in self.toolset.tools or name in META_NAMES:
                continue
            if name in self._mcp_tools or len(self._mcp_tools) < MAX_OFFERED_FOUND:
                self._mcp_tools[name] = _as_mcp(spec)
                self._hidden.pop(name, None)
                rows.append({"name": name, "description": spec.description[:300]})
        if not rows:
            return json.dumps({"tools": [], "note": "no matching tool; say so plainly"})
        return json.dumps({"tools": rows, "note": "these tools can now be called by name"})

    def _rank_hidden(self, query: str, limit: int) -> list[ToolSpec]:
        words = {w for w in query.lower().split() if len(w) > 2}
        scored = [
            (len(words & set(f"{t.name} {t.description}".lower().replace("_", " ").split())), t)
            for t in self._hidden.values()
        ]
        return [t for score, t in sorted(scored, key=lambda x: -x[0])[:limit] if score]

    def _specs(self) -> list[dict]:
        specs = [t.spec() for t in self.toolset.tools.values()]
        for t in self._mcp_tools.values():
            fn = {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}
            specs.append({"type": "function", "function": fn})
        if self.user_mcp:
            for t in self.user_mcp.tools():
                specs.append({"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.schema}})  # fmt: skip
        if self.backend:
            specs.append(FIND_SPEC)
        return specs

    def _offered(self) -> dict[str, dict]:
        offered = {
            n: {"properties": t.properties, "required": t.required} for n, t in self.toolset.tools.items()
        }
        for n, t in self._mcp_tools.items():
            schema = t.get("inputSchema") or {}
            offered[n] = {"properties": schema.get("properties", {}), "required": schema.get("required", [])}
        if self.user_mcp:
            for t in self.user_mcp.tools():
                offered[t.name] = {
                    "properties": t.schema.get("properties", {}),
                    "required": t.schema.get("required", []),
                }
        if self.backend:
            offered[FIND_NAME] = {"properties": FIND_PROPS, "required": ["query"]}
        return offered

    def _budget(self) -> Budget:
        s, t = self.settings, self.settings.tuning
        return Budget(
            s.max_tool_calls, s.max_turns, float(s.max_wall_s), warn_calls=t.warn_calls,
            max_calls_per_response=t.max_calls_per_response, max_consecutive_nudges=t.max_consecutive_nudges,
            loop_repeat_limit=t.loop_repeat_limit, max_consecutive_tool_errors=t.max_consecutive_tool_errors,
        )  # fmt: skip

    def _system_prompt(self) -> str:
        if self.assistant:
            return system_prompt(
                user=self.assistant.user_name, persona=self.assistant.persona,
                channel=self.assistant.channel, timezone=self.assistant.timezone,
                has_tools=self.backend is not None, profile=self._profile_text,
            )  # fmt: skip
        task = "finish" in self.toolset.tools
        lines = ""
        if self.settings.tool_format == "prompt":
            lines = "\n".join(f"- {_describe(s['function'])}" for s in self._specs())
        return render_code_prompt(
            self._budget(),
            task=task,
            workdir=str(self.toolset.workspace.root),
            extras=self.system_extra,
            tool_lines=lines,
        )

    def close(self) -> None:
        """Closes the hub connection, if any."""
        if self.backend:
            self.backend.close()
        if self.user_mcp:
            self.user_mcp.close()
        if self.hooks:
            self.hooks.lifecycle("session_stop")
            self.hooks = None  # close may run twice; stop hooks run once

    def _execute(self, name: str, raw_args: object) -> str:
        fixed = repair_call(name, raw_args, self._offered())
        if fixed.error:
            self.counters["unknown" if fixed.error[0] == "unknown_tool" else "malformed"] += 1
            return render_error(ToolError(*fixed.error)) + "".join(f"\n{n}" for n in fixed.notes)
        if fixed.repairs:
            self.counters["repaired"] += 1
            log.info("repaired %s: %s", name, "; ".join(fixed.repairs))
        name, args = fixed.name, fixed.args
        if self.hooks and (reason := self.hooks.pre_tool(name, args)):
            return render_error(ToolError("hook_blocked", reason))
        try:
            if name in self.toolset.tools:
                out = self.toolset.tools[name].fn(args)
            elif self.user_mcp and name in {t.name for t in self.user_mcp.tools()}:
                out, _ = self.user_mcp.call(name, args)
            elif name == FIND_NAME and self.backend:
                out = self._find_tools(args)
            elif name in self._mcp_tools and self.backend:
                out = self.backend.call(name, args).for_model()
            else:
                return render_error(ToolError("unknown_tool", f"no tool named {name!r}"))
        except ToolError as exc:
            log.info("tool %s failed: %s", name, exc)
            out = render_error(exc)
        except (McpError, ValueError, OSError) as exc:
            log.info("tool %s failed: %s", name, exc)
            out = render_error(ToolError("internal", str(exc)))
        cap = self.settings.tuning.max_result_chars
        if len(out) > cap:
            head = cap // HEAD_SHARE
            out = (
                out[:head]
                + f"\n[... {len(out) - cap} characters cut; to see the middle use read with start_line"
                + " or grep for the part you need ...]\n"
                + out[-(cap - head) :]
            )
        if self.hooks and (note := self.hooks.post_tool(name, args, out)):
            out += f"\n{note}"
        return out + "".join(f"\n{n}" for n in fixed.notes)

    def run(self, prompt: str, images: list[dict] | None = None) -> RunResult:
        """Runs one user prompt (with optional `{mimeType, data}` images) until it ends or a limit hits."""
        if images is None and "@" in prompt:
            images = extract_images(prompt, self.toolset.workspace.root)
        if images and not self.accepts_images:
            raise ValueError(
                f"{self.settings.model} does not take image input (text-only profile or endpoint); "
                "remove the @image reference or switch to a vision model"
            )
        if self.hooks and (reason := self.hooks.user_prompt_submit(prompt)):
            return RunResult(f"Prompt blocked by a hook: {reason}", "blocked", 0, 0)
        if self.split:
            result = run_split(self, prompt, images or [])
        else:
            result = self._run(prompt, images or [])
        if self.turn == 1:
            self._retitle(prompt, result.text)
        if self.session:
            stats = {"model_requests": result.turns, "tool_calls": result.tool_calls, **self.counters}
            self.session.write({"type": "turn_end", "turn": self.turn, "stop_reason": result.stopped,
                                "final_text": result.text, "stats": stats})  # fmt: skip
        return result

    def _run(self, prompt: str, images: list[dict]) -> RunResult:
        s = self.settings
        native = s.tool_format == "native"
        self.cancel.clear()
        self.turn += 1
        self.note.task = prompt
        if self.assistant and self.backend:
            self._select_for_turn(prompt)
        if self.turn == 1 and self.session:
            self.session.write({"type": "title", "title": title_from_prompt(prompt)})
        self._add({"role": "user", "content": prompt, **({"images": images} if images else {})})
        self._constrain()
        budget = self._budget()
        clock, dog = BudgetClock(budget), Watchdog(budget)
        turns = nudges = 0
        text = ""
        task_mode = "finish" in self.toolset.tools
        retried = False
        while True:
            if self.cancel.is_set():
                return RunResult(text, "cancelled", turns, clock.calls_used)
            if turns >= budget.max_model_requests:
                return RunResult(text, "max_model_requests", turns, clock.calls_used)
            if clock.seconds_left <= 0:
                return RunResult(text, "max_time", turns, clock.calls_used)
            turns += 1
            try:
                history = self._fit()
            except ContextOverflow as exc:
                log.error("%s", exc)
                return RunResult(text, "context_overflow", turns, clock.calls_used)
            try:
                started = _now()
                reply = self.provider.complete(self._wire(history), self._specs() if native else None)
                seconds = _now() - started
            except ProviderError as exc:
                if exc.status != 400 or "context_length_exceeded" not in str(exc):
                    raise
                # the gateway's overflow rewrite (05-gateway): our estimate was too low, plan for less
                self.counters["overflows"] += 1
                self.context.input_budget = int(self.context.input_budget * OVERFLOW_SHRINK)
                log.warning(
                    "context overflow reported by the server; input budget now %d", self.context.input_budget
                )
                try:
                    history = self._fit()
                    started = _now()
                    reply = self.provider.complete(self._wire(history), self._specs() if native else None)
                    seconds = _now() - started
                except (ContextOverflow, ProviderError) as again:
                    log.error("%s", again)
                    return RunResult(text, "context_overflow", turns, clock.calls_used)
            self._report_usage(reply, seconds)
            self._calibrate(history, reply, native)
            text = reply.text
            if reply.reasoning.strip():
                self.on_update({"kind": "thinking", "text": reply.reasoning.strip()})
            calls = list(reply.tool_calls)
            if not calls:
                calls, text = self._leaked_calls(text, turns)
            if not calls and self._constraint == "json_schema":
                text = unwrap_answer(text)
            if self.claims and not calls:
                verdict = self.claims.decide(text, tools_run=clock.calls_used, retried=retried)
                if verdict.action == "retry":
                    retried = True  # the held reply is dropped from history and never shown
                    self._add({"role": "system", "content": verdict.system_message})
                    continue
                text = verdict.text
            if text and native and (not self.claims or calls == [] or self.claims.releasable(text)):
                self.on_event("text", text)
                self.on_update({"kind": "text", "text": text})
            if not calls:
                case = self._nudge_case(reply, text, task_mode)
                if not case:
                    self._add({"role": "assistant", "content": text})
                    return RunResult(text, "done", turns, clock.calls_used)
                nudges += 1
                self.counters["nudges"] += 1
                if case == "truncated":
                    self.counters["truncated"] += 1
                if nudges >= budget.max_consecutive_nudges:
                    return RunResult(text, "nudge_limit", turns, clock.calls_used)
                log.warning("nudging after a %s reply (%d)", case, nudges)
                self._add({"role": "assistant", "content": reply.text or "(no reply)"})
                self._add({"role": "user", "content": nudge_text(case)})
                continue
            nudges = 0
            self._add(self._assistant_message(text if native else reply.text, calls, native))
            stop = self._run_calls(calls, native, clock, dog)
            if stop:
                return RunResult(stop[1], stop[0], turns, clock.calls_used)

    def _calibrate(self, history: list[dict], reply: Completion, native: bool) -> None:
        """Sets the context estimate's ratio from the server's real prompt count for this request."""
        if reply.prompt_tokens <= 0:
            return
        chars = len(json.dumps(self._wire(history), ensure_ascii=False))
        if native:
            chars += len(json.dumps(self._specs(), ensure_ascii=False))
        self.tokens.observe(chars, reply.prompt_tokens)
        # only ever more cautious than the default: a server that under-reports (prefix cache,
        # fake servers) must not make the window check trust a too-small estimate
        self.context.chars_per_token = min(CHARS_PER_TOKEN, 1 / (self.tokens.ratio * ESTIMATE_FACTOR))

    def _report_usage(self, reply: Completion, seconds: float = 0.0) -> None:
        """Emits a `usage` update when the server reported token counts."""
        if reply.prompt_tokens or reply.completion_tokens:
            self.on_update(
                {
                    "kind": "usage",
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "window": self.settings.context_window,
                    "context_used": reply.prompt_tokens + reply.completion_tokens,
                    "tokens_per_s": round(reply.completion_tokens / seconds, 1) if seconds > 0 else 0.0,
                }
            )

    @staticmethod
    def _nudge_case(reply: Completion, text: str, task_mode: bool) -> str:
        """Which nudge a call-less reply earns, or an empty string when it is a final answer."""
        if reply.finish == "length" and has_open_marker(reply.text):
            return "truncated"
        if not text.strip():
            return "empty"
        return "finish" if task_mode else ""

    def _run_calls(
        self, calls: list[tuple[str, str, str]], native: bool, clock: BudgetClock, dog: Watchdog
    ) -> tuple[str, str] | None:
        """Runs one response's calls; returns (stop reason, text) to end the turn, else None.

        Every call gets a result, also the ones that do not run, so the history stays paired.
        """
        for n, (cid, name, raw) in enumerate(calls):
            reason = self._pre_check(n, cid, name, raw, clock, dog)
            if isinstance(reason, tuple):  # (stop, error JSON): refuse this call, end the turn
                self._skip(calls[n:], native, reason[1])
                return reason[0], ""
            if reason:  # an error JSON: the call does not run but the turn goes on
                self._record(cid, name, raw, reason, native, run=False)
                continue
            out = self._execute(name, raw)
            if name != "finish":
                out += clock.warning_line()
            result = self._record(cid, name, raw, out, native, run=True)
            if not self._is_free(name):
                clock.calls_used += 1
            if name == "finish" and not result.startswith('{"status": "error"'):
                self._skip(calls[n + 1 :], native, _error("skipped", "the task is finished"))
                return "finished", result
            if dog.result(result.startswith('{"status": "error"')):
                self._skip(calls[n + 1 :], native, _error("skipped", "the turn was stopped"))
                return "tool_errors", ""
        return None

    def _pre_check(self, n: int, cid: str, name: str, raw: str, clock: BudgetClock, dog: Watchdog):
        """None to run the call, an error JSON to refuse it, or (stop reason, error JSON) to stop."""
        free = self._is_free(name)
        if self.cancel.is_set():
            return "cancelled", _error("cancelled", "the turn was cancelled")
        if n >= clock.budget.max_calls_per_response:
            return _error("too_many_calls", f"at most {clock.budget.max_calls_per_response} calls per reply")
        if not free and clock.calls_left <= 0:
            return "max_tool_calls", _error("budget", "the tool-call budget is used up")
        denied = self._review(cid, name, raw)
        if denied:
            return denied
        verdict = dog.observe(canonical(name, raw))
        if verdict == "stop":
            return "loop", _error("loop", "the turn was stopped: the same call keeps repeating")
        if verdict == "refuse":
            self.counters["loop_refusals"] += 1
            return loop_message(name, parse_args(raw) or raw, dog.repeats)
        return None

    def switch_model(self, name: str) -> None:
        """Uses `name` from the next model call on: provider, profile and limits are re-resolved.

        Raises `ValueError` for an empty or oversized name; do not call it during a turn. The name
        only reaches the model socket, which forwards to the one configured model server.
        """
        name = self.resolve_model(name)
        if not name.strip() or len(name) > MAX_MODEL_NAME or any(c.isspace() or ord(c) < 32 for c in name):
            raise ValueError(f"not a model name: {name[:40]!r}")
        requested = replace(self._requested, model=name)
        if hasattr(self.provider, "model"):
            self.provider.model = name
        info = self.provider.model_info() if hasattr(self.provider, "model_info") else ModelInfo()
        self.profile = resolve_profile(
            name, user_file=paths.config_dir() / "model-profiles.toml", hal_profile=info.hal_profile
        )
        if self._built:
            self.provider.extra = self.profile.request_extras()
        self._requested = requested
        self.settings = effective_settings(requested, self.profile, info)
        if self._built and hasattr(self.provider, "max_output"):
            self.provider.max_output = self.settings.max_output

    def _review(self, cid: str, name: str, raw: str) -> str | tuple[str, str] | None:
        """In review mode asks before a gated tool; returns an error JSON when it is refused."""
        tool = self.toolset.tools.get(name)
        if self.mode == "plan" and tool and tool.kind in GATED_KINDS:
            return _error(
                "plan_mode",
                f"{name} is not allowed in plan mode, which is read-only. Read, search and record the steps "
                "with the todo tool, then tell the user the plan is ready so they can leave plan mode.",
            )
        if self.mode != "review" or not self.permission or not tool or tool.kind not in GATED_KINDS:
            return None
        choice = self._remembered.get(name) or self.permission(cid, name, tool.kind, parse_args(raw) or {})
        if choice in ("allow_always", "reject_always"):
            self._remembered[name] = choice
        if choice == CANCELLED:
            self.cancel.set()
            return "cancelled", _error("cancelled", "the turn was cancelled")
        if choice in ALLOW:
            return None
        return _error("denied", f"the user did not allow this {name} call. Do not retry it unchanged.")

    def _is_free(self, name: str) -> bool:
        """True for tools that do not count toward the tool-call budget."""
        tool = self.toolset.tools.get(name)
        return bool(tool and tool.free) or name in FREE_TOOLS

    def _skip(self, calls: list[tuple[str, str, str]], native: bool, error: str) -> None:
        """Adds a synthetic result for each call that will not run."""
        for cid, name, _ in calls:
            self._add(self._result_message(cid, name, error, native))

    def _record(self, cid: str, name: str, raw: str, result: str, native: bool, *, run: bool) -> str:
        """Announces a call and its result to the listeners and the history; returns the result."""
        self.on_event("tool", f"{name} {raw}")
        self.on_update({"kind": "tool_call", "id": cid, "name": name, "raw": raw})
        failed = result.startswith('{"status": "error"')
        self.on_update({"kind": "tool_result", "id": cid, "name": name, "text": result, "failed": failed})
        if run and name == "todo" and not failed:
            self.on_update({"kind": "plan", "text": result})
        if run:
            self.note.observe(name, parse_args(raw) or {}, result)
        self._add(self._result_message(cid, name, result, native))
        return result

    def resolve_model(self, name: str) -> str:
        """The model id for a role name (`main`, `fast`, ...) or `name` itself when it is not a role."""
        if name == "main":
            return self._main_model
        return self._requested.models.get(name, name)

    def _fast_provider(self) -> ChatProvider:
        """The provider for the `fast` role, or the main one when no fast model is configured."""
        fast = self._requested.models.get("fast", "")
        if not fast or fast == self.settings.model or not self._built:
            return self.provider
        if self._fast is None:
            self._fast = make_provider(replace(self._requested, model=fast))
        return self._fast

    def _retitle(self, prompt: str, answer: str) -> None:
        """Writes a short title made by the fast model; the first-prompt title stays on any failure."""
        if not self.session or self._fast_provider() is self.provider:
            return
        request = [
            {"role": "system", "content": TITLE_PROMPT},
            {"role": "user", "content": f"Request: {prompt[:400]}\n\nAnswer: {answer[:400]}"},
        ]
        try:
            title = self._fast_provider().complete(request, None).text.strip().strip("\"'`").splitlines()[0]
        except (ProviderError, IndexError):
            log.info("title generation failed; keeping the first-prompt title", exc_info=True)
            return
        if title:
            self.session.write({"type": "title", "title": title[:TITLE_MAX_CHARS]})

    def _constrain(self) -> None:
        """Passes the profile's grammar or schema for tool calls to the provider (text fallback only)."""
        mode = str(self.profile.values.get("tool_constraint", "none"))
        self._constraint = mode if self.settings.tool_format == "prompt" else "none"
        names = sorted(self._offered())
        keys = request_keys(self._constraint, names) if names else {}
        self.provider.extra = {
            k: v for k, v in self.provider.extra.items() if k not in ("grammar", "response_format")
        } | keys

    def _leak_order(self) -> tuple[str, ...] | None:
        """The profile's parser order, or None for the built-in default."""
        parsers = self.profile.values.get("leak_parsers")
        return tuple(parsers) if parsers else None

    def _leaked_calls(self, text: str, turn: int) -> tuple[list[tuple[str, str, str]], str]:
        """Calls the model wrote into its text; returns them and the text without them."""
        order = ("hermes",) if self.settings.tool_format == "prompt" else self._leak_order()
        parser, repaired, rest = repair_text(text, self._offered(), order)
        if not repaired:
            return [], text
        log.info("parsed %d leaked call(s) with %s", len(repaired), parser)
        return [(f"t{turn}_{i}", r.name, json.dumps(r.args)) for i, r in enumerate(repaired)], rest

    @staticmethod
    def _assistant_message(text: str, calls: list[tuple[str, str, str]], native: bool) -> dict:
        if not native:
            return {"role": "assistant", "content": text}
        return {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": n, "arguments": a}}
                for cid, n, a in calls
            ],
        }

    @staticmethod
    def _result_message(cid: str, name: str, result: str, native: bool) -> dict:
        if native:
            return {"role": "tool", "tool_call_id": cid, "content": result}
        return {"role": "user", "content": f"[tool result for {name}]\n{result}"}

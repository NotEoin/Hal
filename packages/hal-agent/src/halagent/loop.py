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
from collections.abc import Callable
from dataclasses import dataclass

from halagent.budget import (
    FREE_TOOLS,
    Budget,
    BudgetClock,
    Watchdog,
    canonical,
    loop_message,
    nudge_text,
)
from halagent.context import (
    ContextManager,
    ContextOverflow,
    StateNote,
    compaction_prompt,
    render_for_summary,
)
from halagent.mcp.lite import LiteMcpClient, McpError
from halagent.profiles import Profile, resolve_profile
from halagent.provider import ChatProvider, Completion
from halagent.repair import has_open_marker, repair_call, repair_text
from halagent.repair.aliases import parse_args
from halagent.sessions import SessionWriter, replay_indexed
from halagent.settings import AgentSettings
from halagent.tools import ToolError, Toolset, render_error
from halkit import paths

log = logging.getLogger("halagent.loop")

MAX_RESULT_CHARS = 12_000
HEAD_KEEP = 4_000
TAIL_KEEP = 8_000
SYSTEM = (
    "You are a coding agent. Work inside the workspace using the tools. Read before you edit, "
    "check your work by running tests, and answer briefly when the task is done."
)
PROMPT_FORMAT = (
    "\nTo call a tool, reply with one or more blocks of exactly this form and nothing else:\n"
    '<tool_call>{"name": "TOOL", "arguments": {"ARG": "VALUE"}}</tool_call>\n'
    "Tool results come back in the next message. Tools:\n"
)


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
    ) -> None:
        self.settings = settings
        self.profile = profile or resolve_profile(
            settings.model, user_file=paths.config_dir() / "model-profiles.toml"
        )
        self.system_extra = system_extra
        self.on_update = on_update or (lambda update: None)
        self.cancel = threading.Event()
        self.toolset = toolset
        self.provider = provider or ChatProvider(
            settings.endpoint,
            settings.model,
            api_key=settings.api_key,
            stream=settings.stream,
            extra=self.profile.request_extras(),
        )
        self.on_event = on_event or (lambda kind, text: None)
        self.mcp: LiteMcpClient | None = None
        self._mcp_tools: dict[str, dict] = {}
        if settings.broker:
            self.mcp = LiteMcpClient(settings.broker)
            self.mcp.initialize()
            self._mcp_tools = {t["name"]: t for t in self.mcp.list_tools() if t["name"] not in toolset.tools}
        self.session = session
        self.turn = 0
        self._next_i = 0
        self.note = StateNote()
        self.context = ContextManager(settings.context_window, settings.max_output, self._summarize)
        self._ids: list[int] = []
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
        request = [
            {"role": "system", "content": compaction_prompt()},
            {"role": "user", "content": render_for_summary(old)},
        ]
        return self.provider.complete(request, None).text

    def _fit(self) -> list[dict]:
        """The history to send; compacts it (and records that) when it is over budget."""
        tools_chars = len(json.dumps(self._specs())) if self.settings.tool_format == "native" else 0
        fitted = self.context.fit(self.messages, self._ids, self.note, tools_chars)
        self.messages, self._ids = fitted.messages, fitted.ids
        for rec in fitted.records:
            if rec["kind"] in ("summary", "note"):
                rec = {**rec, "summary_i": self._next_i}
            if self.session:
                self.session.write({**rec, "turn": self.turn, "id": f"cmp_{self._next_i}"})
        if fitted.summary_at is not None:
            self._ids[fitted.summary_at] = self._next_i
            if self.session:
                msg = self.messages[fitted.summary_at]
                self.session.write({"type": "message", "turn": self.turn, "i": self._next_i, "msg": msg})
            self._next_i += 1
        return self.messages

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

    def _specs(self) -> list[dict]:
        specs = [t.spec() for t in self.toolset.tools.values()]
        for t in self._mcp_tools.values():
            fn = {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}
            specs.append({"type": "function", "function": fn})
        return specs

    def _offered(self) -> dict[str, dict]:
        offered = {
            n: {"properties": t.properties, "required": t.required} for n, t in self.toolset.tools.items()
        }
        for n, t in self._mcp_tools.items():
            schema = t.get("inputSchema") or {}
            offered[n] = {"properties": schema.get("properties", {}), "required": schema.get("required", [])}
        return offered

    def _system_prompt(self) -> str:
        base = SYSTEM + (f"\n\n{self.system_extra}" if self.system_extra else "")
        if self.settings.tool_format != "prompt":
            return base
        lines = [
            f"- {s['function']['name']}: {s['function']['description']} "
            f"arguments: {json.dumps(s['function']['parameters'].get('properties', {}))}"
            for s in self._specs()
        ]
        return base + PROMPT_FORMAT + "\n".join(lines)

    def close(self) -> None:
        """Closes the hub connection, if any."""
        if self.mcp:
            self.mcp.close()

    def _execute(self, name: str, raw_args: object) -> str:
        fixed = repair_call(name, raw_args, self._offered())
        if fixed.error:
            return render_error(ToolError(*fixed.error)) + "".join(f"\n{n}" for n in fixed.notes)
        if fixed.repairs:
            log.info("repaired %s: %s", name, "; ".join(fixed.repairs))
        name, args = fixed.name, fixed.args
        try:
            if name in self.toolset.tools:
                out = self.toolset.tools[name].fn(args)
            elif name in self._mcp_tools and self.mcp:
                text, is_error = self.mcp.call_tool(name, args)
                out = ("error: " if is_error else "") + text
            else:
                return render_error(ToolError("unknown_tool", f"no tool named {name!r}"))
        except ToolError as exc:
            log.info("tool %s failed: %s", name, exc)
            out = render_error(exc)
        except (McpError, ValueError, OSError) as exc:
            log.info("tool %s failed: %s", name, exc)
            out = render_error(ToolError("internal", str(exc)))
        if len(out) > MAX_RESULT_CHARS:
            out = (
                out[:HEAD_KEEP]
                + f"\n[... {len(out) - MAX_RESULT_CHARS} characters cut ...]\n"
                + out[-TAIL_KEEP:]
            )
        return out + "".join(f"\n{n}" for n in fixed.notes)

    def run(self, prompt: str) -> RunResult:
        """Runs one user prompt to completion or until a limit is hit."""
        result = self._run(prompt)
        if self.session:
            stats = {"model_requests": result.turns, "tool_calls": result.tool_calls}
            self.session.write({"type": "turn_end", "turn": self.turn, "stop_reason": result.stopped,
                                "final_text": result.text, "stats": stats})  # fmt: skip
        return result

    def _run(self, prompt: str) -> RunResult:
        s = self.settings
        native = s.tool_format == "native"
        self.cancel.clear()
        self.turn += 1
        self.note.task = prompt
        self._add({"role": "user", "content": prompt})
        budget = Budget(s.max_tool_calls, s.max_turns, float(s.max_wall_s))
        clock, dog = BudgetClock(budget), Watchdog(budget)
        turns = nudges = 0
        text = ""
        task_mode = "finish" in self.toolset.tools
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
            reply = self.provider.complete(history, self._specs() if native else None)
            self._report_usage(reply)
            text = reply.text
            calls = list(reply.tool_calls)
            if not calls:
                calls, text = self._leaked_calls(text, turns)
            if text and native:
                self.on_event("text", text)
                self.on_update({"kind": "text", "text": text})
            if not calls:
                case = self._nudge_case(reply, text, task_mode)
                if not case:
                    self._add({"role": "assistant", "content": text})
                    return RunResult(text, "done", turns, clock.calls_used)
                nudges += 1
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

    def _report_usage(self, reply: Completion) -> None:
        """Emits a `usage` update when the server reported token counts."""
        if reply.prompt_tokens or reply.completion_tokens:
            self.on_update(
                {
                    "kind": "usage",
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "window": self.settings.context_window,
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
            reason = self._pre_check(n, name, raw, clock, dog)
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

    def _pre_check(self, n: int, name: str, raw: str, clock: BudgetClock, dog: Watchdog):
        """None to run the call, an error JSON to refuse it, or (stop reason, error JSON) to stop."""
        free = self._is_free(name)
        if self.cancel.is_set():
            return "cancelled", _error("cancelled", "the turn was cancelled")
        if n >= clock.budget.max_calls_per_response:
            return _error("too_many_calls", f"at most {clock.budget.max_calls_per_response} calls per reply")
        if not free and clock.calls_left <= 0:
            return "max_tool_calls", _error("budget", "the tool-call budget is used up")
        verdict = dog.observe(canonical(name, raw))
        if verdict == "stop":
            return "loop", _error("loop", "the turn was stopped: the same call keeps repeating")
        if verdict == "refuse":
            return loop_message(name, parse_args(raw) or raw, dog.repeats)
        return None

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
        return {"role": "user", "content": f"<tool_result name={name!r}>\n{result}\n</tool_result>"}

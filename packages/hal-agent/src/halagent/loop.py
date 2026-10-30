"""The agent loop: model call, tool calls, repeat, bounded by a turn and a tool-call limit.

Tools come from the workspace and, when a hub socket is configured, from MCP; a built-in tool
wins a name clash. With `tool_format = "prompt"` the tools are described in the system prompt
and the model calls them with `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`
(for models without native tool calls). Tool errors go back to the model, never raise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from halagent.mcp.lite import LiteMcpClient, McpError
from halagent.provider import ChatProvider
from halagent.repair import repair_call, repair_text
from halagent.sessions import SessionWriter, replay
from halagent.settings import AgentSettings
from halagent.tools import ToolError, Toolset, render_error

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


@dataclass
class RunResult:
    """Outcome of one prompt: final text, why it stopped, and counts."""

    text: str
    stopped: str  # "done", "finished", "turn_limit" or "tool_limit"
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
    ) -> None:
        self.settings = settings
        self.system_extra = system_extra
        self.toolset = toolset
        self.provider = provider or ChatProvider(
            settings.endpoint, settings.model, api_key=settings.api_key, stream=settings.stream
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
        self.messages: list[dict] = []
        self._add({"role": "system", "content": self._system_prompt()})

    def _add(self, msg: dict, **extra: object) -> None:
        """Appends a message to the history and, with a session, to the transcript."""
        self.messages.append(msg)
        if self.session:
            rec = {"type": "message", "turn": self.turn, "i": self._next_i, "msg": msg, **extra}
            self.session.write(rec)
        self._next_i += 1

    def resume(self, records: list[dict]) -> None:
        """Continues a stored session: replaces the history with the replayed messages."""
        replayed = replay(records)
        if not replayed or replayed[0].get("role") != "system":
            raise ValueError("the stored session has no system message")
        replayed[0] = self.messages[0]  # the current prompt wins over the stored one
        self.messages = replayed
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
        self.turn += 1
        self._add({"role": "user", "content": prompt})
        turns = calls_made = 0
        text = ""
        while turns < s.max_turns:
            turns += 1
            reply = self.provider.complete(self.messages, self._specs() if native else None)
            text = reply.text
            calls = list(reply.tool_calls)
            if not calls:
                calls, text = self._leaked_calls(text, turns)
            if text and native:
                self.on_event("text", text)
            if not calls:
                self._add({"role": "assistant", "content": text})
                return RunResult(text, "done", turns, calls_made)
            self._add(self._assistant_message(text if native else reply.text, calls, native))
            for cid, name, raw in calls:
                tool = self.toolset.tools.get(name)
                free = bool(tool and tool.free)
                if not free and calls_made >= s.max_tool_calls:
                    return RunResult(text, "tool_limit", turns, calls_made)
                calls_made += 0 if free else 1
                self.on_event("tool", f"{name} {raw}")
                result = self._execute(name, raw)
                self._add(self._result_message(cid, name, result, native))
                if name == "finish" and not result.startswith('{"status": "error"'):
                    return RunResult(result, "finished", turns, calls_made)
        return RunResult(text, "turn_limit", turns, calls_made)

    def _leaked_calls(self, text: str, turn: int) -> tuple[list[tuple[str, str, str]], str]:
        """Calls the model wrote into its text; returns them and the text without them."""
        order = ("hermes",) if self.settings.tool_format == "prompt" else None
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

"""The agent loop: model call, tool calls, repeat, bounded by a turn and a tool-call limit.

Tools come from the workspace and, when a hub socket is configured, from MCP; a built-in tool
wins a name clash. With `tool_format = "prompt"` the tools are described in the system prompt
and the model calls them with `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`
(for models without native tool calls). Tool errors go back to the model, never raise.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from halagent.mcp.lite import LiteMcpClient, McpError
from halagent.provider import ChatProvider
from halagent.settings import AgentSettings
from halagent.tools import ToolError, Workspace

log = logging.getLogger("halagent.loop")

MAX_RESULT_CHARS = 12_000
CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
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
    stopped: str  # "done", "turn_limit" or "tool_limit"
    turns: int
    tool_calls: int


class Agent:
    """A conversation with tools. `run` may be called repeatedly; history persists."""

    def __init__(
        self,
        settings: AgentSettings,
        workspace: Workspace,
        provider: ChatProvider | None = None,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.provider = provider or ChatProvider(
            settings.endpoint, settings.model, api_key=settings.api_key, stream=settings.stream
        )
        self.on_event = on_event or (lambda kind, text: None)
        self.mcp: LiteMcpClient | None = None
        self._mcp_tools: dict[str, dict] = {}
        if settings.broker:
            self.mcp = LiteMcpClient(settings.broker)
            self.mcp.initialize()
            self._mcp_tools = {
                t["name"]: t for t in self.mcp.list_tools() if t["name"] not in workspace.tools
            }
        self.messages: list[dict] = [{"role": "system", "content": self._system_prompt()}]

    def _specs(self) -> list[dict]:
        specs = [t.spec() for t in self.workspace.tools.values()]
        for t in self._mcp_tools.values():
            fn = {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}
            specs.append({"type": "function", "function": fn})
        return specs

    def _system_prompt(self) -> str:
        if self.settings.tool_format != "prompt":
            return SYSTEM
        lines = [
            f"- {s['function']['name']}: {s['function']['description']} "
            f"arguments: {json.dumps(s['function']['parameters'].get('properties', {}))}"
            for s in self._specs()
        ]
        return SYSTEM + PROMPT_FORMAT + "\n".join(lines)

    def close(self) -> None:
        """Closes the hub connection, if any."""
        if self.mcp:
            self.mcp.close()

    def _execute(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            return f"error: bad arguments: {exc}"
        try:
            if name in self.workspace.tools:
                out = self.workspace.tools[name].fn(args)
            elif name in self._mcp_tools and self.mcp:
                text, is_error = self.mcp.call_tool(name, args)
                out = ("error: " if is_error else "") + text
            else:
                return f"error: unknown tool {name!r}"
        except (ToolError, McpError, ValueError, OSError) as exc:
            log.info("tool %s failed: %s", name, exc)
            return f"error: {exc}"
        return out if len(out) <= MAX_RESULT_CHARS else out[:MAX_RESULT_CHARS] + "\n[result truncated]"

    def run(self, prompt: str) -> RunResult:
        """Runs one user prompt to completion or until a limit is hit."""
        s = self.settings
        native = s.tool_format == "native"
        self.messages.append({"role": "user", "content": prompt})
        turns = calls_made = 0
        text = ""
        while turns < s.max_turns:
            turns += 1
            reply = self.provider.complete(self.messages, self._specs() if native else None)
            text = reply.text
            calls = list(reply.tool_calls)
            if not native:
                calls = self._parse_text_calls(text, turns)
            if text and native:
                self.on_event("text", text)
            if not calls:
                self.messages.append({"role": "assistant", "content": text})
                return RunResult(text, "done", turns, calls_made)
            self.messages.append(self._assistant_message(text, calls, native))
            for cid, name, raw in calls:
                if calls_made >= s.max_tool_calls:
                    return RunResult(text, "tool_limit", turns, calls_made)
                calls_made += 1
                self.on_event("tool", f"{name} {raw}")
                result = self._execute(name, raw)
                self.messages.append(self._result_message(cid, name, result, native))
        return RunResult(text, "turn_limit", turns, calls_made)

    @staticmethod
    def _parse_text_calls(text: str, turn: int) -> list[tuple[str, str, str]]:
        out = []
        for i, m in enumerate(CALL_RE.finditer(text)):
            try:
                obj = json.loads(m.group(1))
                out.append((f"t{turn}_{i}", str(obj["name"]), json.dumps(obj.get("arguments") or {})))
            except (ValueError, KeyError, TypeError):
                out.append((f"t{turn}_{i}", "?", "{}"))
        return out

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

"""Conversation helpers shared by the policies, ported from `hal-route.py` (REQ-GW-41).

Both wire formats are handled. An Anthropic tool result is a `tool_result` block in a user
message; an OpenAI one is a `tool` role message whose tool name comes from the matching
`tool_calls[].id` of the assistant message before it, and it has no `is_error`, so only the
failure regexes apply to it. Design decisions:

- `FAILURE` is checked on shell tool results only: source code read by other tools is full of
  names like `ValueError`.
- Exit code 1 counts as failure except after a search or comparison, where it means no match.
"""

from __future__ import annotations

import hashlib
import json
import re

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)  # context Claude Code injects
AGENT_REPORT = re.compile(r"<agent-message\b|<task-notification\b")  # a subagent report continues the turn
FAILURE = re.compile(
    r"Traceback \(most recent call last\)|\bFAILED\b|\b\d+ failed\b"
    r"|\b[A-Z]\w*Error:|^Exit code (?!1$)[1-9]\d*$",
    re.M,
)
EXIT_1 = re.compile(r"^Exit code 1$", re.M)
SEARCH_COMMAND = re.compile(r"^\s*(grep|egrep|fgrep|rg|ag|git grep|diff|cmp|test|\[)\b")
SHELL_TOOLS = frozenset({"Bash", "run", "bash", "shell"})
AGENT_TOOLS = frozenset({"Agent", "Task"})


def conversation_key(messages: list[dict]) -> str:  # type: ignore[type-arg]
    """Identifies a conversation by its first message, which never changes."""
    first = messages[0] if messages else {}
    return hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()


def _blocks(content: object) -> list[dict]:  # type: ignore[type-arg]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]  # type: ignore[attr-defined]


def block_text(content: object) -> str:
    """The text of a tool result whose content is a string or a list of text blocks."""
    return "\n".join(b.get("text", "") for b in _blocks(content) if b.get("type") == "text")


def user_text(message: dict, fmt: str) -> str:  # type: ignore[type-arg]
    """What the user typed in `message`; "" for a tool result, another role or a subagent report."""
    if message.get("role") != "user":
        return ""
    blocks = _blocks(message.get("content"))
    if any(b.get("type") == "tool_result" for b in blocks):
        return ""
    text = REMINDER.sub("", block_text(blocks)).strip()
    return "" if AGENT_REPORT.search(text) else text


def is_search(command: str) -> bool:
    """True when a shell command ends in a search or comparison."""
    last = re.split(r"(?<!\|)\|(?!\|)", command)[-1]
    return bool(SEARCH_COMMAND.search(last) or SEARCH_COMMAND.search(command))


def _command_of(arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return ""
    return str(arguments.get("command", "")) if isinstance(arguments, dict) else ""


def _anthropic_results(turns: list[dict]) -> list[tuple[str, str, str, bool]]:  # type: ignore[type-arg]
    """`(tool name, command, text, is_error)` for each tool result of the last message."""
    uses = {}
    if len(turns) > 1 and isinstance(turns[-2].get("content"), list):
        uses = {
            b.get("id"): b
            for b in turns[-2]["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        }
    out = []
    for block in _blocks(turns[-1].get("content")):
        if block.get("type") == "tool_result":
            use = uses.get(block.get("tool_use_id"), {})
            out.append(
                (
                    str(use.get("name")),
                    _command_of(use.get("input")),
                    block_text(block.get("content")),
                    bool(block.get("is_error")),
                )
            )
    return out


def _openai_results(turns: list[dict]) -> list[tuple[str, str, str, bool]]:  # type: ignore[type-arg]
    """Tool messages at the end of `turns`, matched to the assistant's `tool_calls` by id."""
    tail = []
    for message in reversed(turns):
        if message.get("role") != "tool":
            break
        tail.append(message)
    calls = {}
    before = turns[len(turns) - len(tail) - 1] if len(turns) > len(tail) else {}
    for call in before.get("tool_calls") or []:
        if isinstance(call, dict):
            calls[call.get("id")] = call.get("function") or {}
    out = []
    for message in reversed(tail):
        function = calls.get(message.get("tool_call_id"), {})
        out.append(
            (
                str(function.get("name")),
                _command_of(function.get("arguments")),
                block_text(message.get("content")),
                False,
            )
        )
    return out


def decision_point(
    messages: list[dict],  # type: ignore[type-arg]
    fmt: str,
    shell_tools: frozenset[str] = SHELL_TOOLS,
    agent_tools: frozenset[str] = AGENT_TOOLS,
) -> str:
    """Why the next reply needs the effort note ("subagent report", ...), or "" for a routine step."""
    turns = [m for m in messages if m.get("role") != "system"]
    last = turns[-1] if turns else {}
    if last.get("role") == "tool" and fmt == "openai":
        results = _openai_results(turns)
    elif last.get("role") == "user":
        results = _anthropic_results(turns)
    else:
        return ""
    if results:
        for name, command, text, is_error in results:
            if name in agent_tools:
                return "subagent report"
            if name in shell_tools:
                if FAILURE.search(text):
                    return "failing command"
                if (is_error or EXIT_1.search(text)) and not is_search(command):
                    return "failing command"
            elif is_error:
                return "tool error"
        return ""
    text = REMINDER.sub("", block_text(last.get("content"))).strip()
    if not text:
        return ""
    return "subagent report" if AGENT_REPORT.search(text) else "user message"


def append_system(body: dict, fmt: str, note: str) -> None:  # type: ignore[type-arg]
    """Adds `note` as a final system message (REQ-GW-41); both formats accept one mid-conversation."""
    body["messages"] = [*(body.get("messages") or []), {"role": "system", "content": note}]

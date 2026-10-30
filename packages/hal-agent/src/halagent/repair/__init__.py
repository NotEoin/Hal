"""Tool-call repair: turns what a small model produced into calls to tools it was offered.

`repair_call` fixes one call (name aliases, argument parsing, aliases and coercion).
`repair_text` finds calls leaked into visible text. Both only ever return calls to offered tools,
so a repaired call passes the same policy and broker checks as a native one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from halagent.repair.aliases import parse_args, repair_args, resolve_name
from halagent.repair.leaks import PARSERS, RawCall, has_open_marker, parse_leaks

__all__ = ["PARSERS", "Repaired", "has_open_marker", "repair_call", "repair_text", "unknown_tool_message"]


@dataclass
class Repaired:
    """One call after repair; `error` is set (and `name` empty) when it cannot be run."""

    name: str
    args: dict
    notes: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    error: tuple[str, str] | None = None  # (error_type, message)


def unknown_tool_message(name: str, offered: Mapping[str, object]) -> str:
    """The message for a name that resolves to no offered tool."""
    return f"There is no tool named {name!r}. Your tools are: {', '.join(offered)}."


def repair_call(name: str, raw_args: object, offered: Mapping[str, Mapping]) -> Repaired:
    """Repairs one call; `offered` maps tool name to `{"properties": ..., "required": [...]}`."""
    args = parse_args(raw_args)
    if args is None:
        return Repaired("", {}, error=("invalid_args", "the arguments are not a valid JSON object"))
    resolved = resolve_name(name, offered, args)
    if resolved is None:
        return Repaired("", {}, error=("unknown_tool", unknown_tool_message(name, offered)))
    repairs = [f"name {name} -> {resolved}"] if resolved != name else []
    schema = offered[resolved]
    fixed, notes = repair_args(
        resolved, args, schema.get("properties", {}), tuple(schema.get("required", ()))
    )
    if fixed != args:
        repairs.append("arguments")
    missing = [k for k in schema.get("required", ()) if k not in fixed]
    if missing:
        need = ", ".join(schema["required"])
        got = ", ".join(fixed) or "nothing"
        return Repaired("", {}, notes, repairs, ("invalid_args", f"{resolved} needs {need}; got {got}."))
    return Repaired(resolved, fixed, notes, repairs)


def repair_text(text: str, offered: Mapping[str, Mapping], order: tuple[str, ...] | None = None):
    """Finds leaked calls in visible text; returns (parser, repaired calls, text without them)."""

    def accept(call: RawCall) -> bool:
        return (
            resolve_name(call.name, offered, call.args if isinstance(call.args, dict) else None) is not None
        )

    kwargs = {"order": order} if order else {}
    parser, calls, rest = parse_leaks(text, accept=accept, **kwargs)
    return parser, [repair_call(c.name, c.args, offered) for c in calls if accept(c)], rest

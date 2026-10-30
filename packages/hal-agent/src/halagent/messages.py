"""Wire-independent message, request and stream types for the agent (design 03, 3.2.1).

Providers translate these to and from each API; the loop, sessions and compaction see only
these types. `ToolCall.arguments` is always a dict after repair, and history stores the
repaired dict, never the raw model string. Every type has `to_json()` and `from_json()` so
transcripts and tests share one serialisation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
ToolKind = Literal["read", "edit", "delete", "move", "search", "execute", "think", "fetch", "other"]
FinishReason = Literal["stop", "tool_calls", "length", "filtered", "refusal", "cancelled"]

ROLES: tuple[str, ...] = ("system", "user", "assistant", "tool")
TOOL_KINDS: tuple[str, ...] = (
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "other",
)
FINISH_REASONS: tuple[str, ...] = (
    "stop",
    "tool_calls",
    "length",
    "filtered",
    "refusal",
    "cancelled",
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as offered to the model."""

    name: str
    description: str
    parameters: dict[str, Any]
    kind: ToolKind = "other"
    source: str = "builtin"  # "builtin", "broker" or "mcp:<server>"
    counts: bool = True  # False: does not count toward max_tool_calls

    def to_json(self) -> dict[str, Any]:
        """Plain-dict form."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "kind": self.kind,
            "source": self.source,
            "counts": self.counts,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ToolSpec:
        """Rebuild from `to_json()` output; unknown kinds fail loudly."""
        kind = data.get("kind", "other")
        if kind not in TOOL_KINDS:
            raise ValueError(f"unknown tool kind: {kind!r}")
        return cls(
            name=data["name"],
            description=data["description"],
            parameters=dict(data["parameters"]),
            kind=kind,
            source=data.get("source", "builtin"),
            counts=bool(data.get("counts", True)),
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One call the model asked for, after repair."""

    id: str  # provider id, or "call_<turn>_<n>" when absent
    name: str
    arguments: dict[str, Any]
    repair: tuple[str, ...] = ()  # for example ("leaked:gemma4", "alias:bash->run")

    def to_json(self) -> dict[str, Any]:
        """Plain-dict form."""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "repair": list(self.repair),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ToolCall:
        """Rebuild from `to_json()` output; non-object arguments fail loudly."""
        args = data.get("arguments", {})
        if not isinstance(args, dict):
            raise ValueError("tool call arguments must be an object")
        return cls(
            id=data["id"],
            name=data["name"],
            arguments=args,
            repair=tuple(data.get("repair", ())),
        )


@dataclass(slots=True)
class Message:
    """One message of the conversation, independent of the wire API.

    `meta` keys in use: `masked` (stub text replaced the content), `incomplete` (the stream
    broke), `summary` (compaction summary), `error_type` (tool messages).
    """

    role: Role
    content: str = ""
    reasoning: str = ""  # assistant only; never shown as text
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str = ""  # tool only
    tool_name: str = ""  # tool only
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role!r}")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages carry tool calls")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("a tool message needs tool_call_id")

    def to_json(self) -> dict[str, Any]:
        """Plain-dict form; empty optional fields are left out."""
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.reasoning:
            out["reasoning"] = self.reasoning
        if self.tool_calls:
            out["tool_calls"] = [c.to_json() for c in self.tool_calls]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            out["tool_name"] = self.tool_name
        if self.meta:
            out["meta"] = dict(self.meta)
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Message:
        """Rebuild from `to_json()` output."""
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            reasoning=data.get("reasoning", ""),
            tool_calls=[ToolCall.from_json(c) for c in data.get("tool_calls", ())],
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
            meta=dict(data.get("meta", {})),
        )


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one model request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    estimated: bool = False

    def to_json(self) -> dict[str, Any]:
        """Plain-dict form."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "estimated": self.estimated,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Usage:
        """Rebuild from `to_json()` output; missing counts are zero."""
        return cls(
            int(data.get("prompt_tokens", 0)),
            int(data.get("completion_tokens", 0)),
            int(data.get("reasoning_tokens", 0)),
            int(data.get("cached_tokens", 0)),
            bool(data.get("estimated", False)),
        )


@dataclass(frozen=True, slots=True)
class Sampling:
    """Sampling knobs; `None` leaves the server default."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repeat_penalty: float | None = None
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Everything a provider needs for one model request."""

    model: str
    messages: Sequence[Message]
    tools: Sequence[ToolSpec] = ()
    max_output: int = 8192
    sampling: Sampling = Sampling()
    thinking: str = "on"  # "on", "off" or an effort name such as "low"
    reasoning_replay: str = "tool_turns"  # "none", "tool_turns" or "all"
    parallel_tool_calls: bool = False
    stop: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)  # profile passthrough per API


@dataclass(slots=True)
class ModelReply:
    """A finished model reply: the message, why it ended and what it cost."""

    message: Message  # role "assistant"
    finish: FinishReason
    usage: Usage = Usage()
    raw_finish: str = ""  # the server's own value, for logs
    first_token_s: float | None = None
    seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.finish not in FINISH_REASONS:
            raise ValueError(f"unknown finish reason: {self.finish!r}")


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of visible reply text."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """A piece of reasoning text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """A fragment of a tool call; fragments with one `index` concatenate."""

    index: int
    id: str
    name: str
    arguments_fragment: str


@dataclass(frozen=True, slots=True)
class Done:
    """Last item of a stream."""

    completion: ModelReply


StreamItem = TextDelta | ReasoningDelta | ToolCallDelta | Done

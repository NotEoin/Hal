"""Effort levels and how each model family and backend expresses them.

Hal has one dial, `off|low|medium|high|max`. Models and servers disagree on how to turn it:
Qwen3 takes a template switch, gpt-oss and OpenAI models take `reasoning_effort`, Anthropic
takes a token budget, DeepSeek-R1 cannot be turned off. This module holds that knowledge as
data (`FAMILIES`) and one function, `request_fields`, that returns the request fields to merge
for a level, a family and a backend. The gateway applies them (docs/user/gateway.md, "Effort").

Design decisions:

- Pure functions, no I/O, so the table is unit-tested.
- A family is found from a hint first (Ollama `details.family`, GGUF `general.architecture`),
  then from the model name. Name patterns are heuristics: an unusual fine-tune may be missed,
  and the role's `family` setting overrides detection.
- A model that cannot think gets no fields and a note, never an error: effort is a hint.
- Fields for `llamacpp` and `ollama` follow those servers' documentation at the time of writing;
  per-request `reasoning_budget` on llama-server and `think` on Ollama's `/v1` route are
  [unverified: check against your server version with a live request].
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVELS = ("off", "low", "medium", "high", "max")
BACKENDS = ("ollama", "openai", "llamacpp", "anthropic", "openai-cloud")
KINDS = ("none", "toggle", "always", "levels", "budget")

# Anthropic `thinking.budget_tokens` per level: 1024 is the API minimum; the larger values
# are Hal's choice (about 2x per step) and stay below the 32000 tokens of the usual max_tokens.
ANTHROPIC_BUDGETS = {"low": 1024, "medium": 4096, "high": 12288, "max": 31999}
# llama-server `reasoning_budget` (thinking tokens) per level; -1 = unlimited, 0 = thinking off
LLAMACPP_BUDGETS = {"off": 0, "low": 1024, "medium": 4096, "high": 16384, "max": -1}
OPENAI_LEVELS = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


@dataclass(frozen=True)
class Capability:
    """Whether and how a model family thinks."""

    kind: str  # one of KINDS
    family: str
    lowest: str = ""  # for kind "levels": what `off` maps to, since thinking cannot be disabled
    note: str = ""


# Checked in order; the first pattern that matches the family hint or the name wins.
FAMILIES: tuple[tuple[re.Pattern[str], Capability], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), cap)
    for pattern, cap in (
        (r"qwen3[-_ ]?coder|instruct-2507", Capability("none", "qwen3-instruct")),
        (r"qwen3|qwen35|qwen3\.\d", Capability("toggle", "qwen3")),
        (r"deepseek[-_]?r1|qwq|r1-distill", Capability("always", "deepseek-r1")),
        (r"gpt[-_]?oss", Capability("levels", "gpt-oss", "low")),
        (r"(^|[/:-])(o1|o3|o4)(-|$)|gpt-5", Capability("levels", "openai-reasoning", "low")),
        (r"claude-(opus|sonnet|haiku)-4|claude-3-7", Capability("budget", "claude")),
    )
)
NO_THINKING = Capability("none", "")


def capability(family_hint: str = "", name: str = "") -> Capability:
    """The capability of a model, from a family hint (architecture) and then its name."""
    for text in (family_hint, name):
        for pattern, cap in FAMILIES:
            if text and pattern.search(text):
                return cap
    return NO_THINKING


NO_EFFORT_REASON = "this model has no thinking control"
ALWAYS_THINKS_REASON = "this model always thinks and cannot be switched"


def levels_for(cap: Capability) -> tuple[tuple[str, ...], str]:
    """`(levels, reason)`: the levels a user may set for `cap`, or none and a one-line reason."""
    if cap.kind == "none":
        return (), NO_EFFORT_REASON
    if cap.kind == "always":
        return (), ALWAYS_THINKS_REASON
    return LEVELS, ""


def parse_level(value: object) -> str | None:
    """A level from a header or body value, or None when it is not one of `LEVELS`."""
    text = str(value).strip().lower()
    return text if text in LEVELS else None


@dataclass(frozen=True)
class Applied:
    """The outcome of mapping a level: fields to merge into the request body, and a note."""

    fields: dict[str, object]
    note: str = ""
    applied: bool = False


def _levels_value(level: str, cap: Capability) -> str:
    return cap.lowest if level == "off" else OPENAI_LEVELS[level]


def request_fields(level: str, cap: Capability, backend: str) -> Applied:
    """The request fields that give `level` to a model of `cap` served by `backend`.

    Raises `ValueError` for an unknown level or backend. A model that cannot think returns no
    fields and says why in `note`; an `always` model cannot be switched off and says so.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown effort level {level!r}; use one of {LEVELS}")
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend kind {backend!r}; use one of {BACKENDS}")
    if cap.kind == "none":
        return Applied({}, "the model does not think; effort ignored")
    if cap.kind == "always":
        note = "the model always thinks" + ("; off cannot be honoured" if level == "off" else "")
        return Applied({}, note)
    on = level != "off"
    if backend == "anthropic":
        if cap.kind != "budget":
            return Applied({}, "this model is not an Anthropic thinking model; effort ignored")
        if not on:
            return Applied({"thinking": {"type": "disabled"}}, applied=True)
        budget = ANTHROPIC_BUDGETS[level]
        thinking = {"type": "enabled", "budget_tokens": budget}
        return Applied({"thinking": thinking}, f"max_tokens must exceed {budget}", True)
    if cap.kind == "budget":
        return Applied({}, "token-budget thinking is available through the anthropic backend only")
    if cap.kind == "levels":
        value = _levels_value(level, cap)
        note = "thinking cannot be turned off; used the lowest level" if level == "off" else ""
        if backend == "ollama":
            return Applied({"think": value, "reasoning_effort": value}, note, True)
        if backend == "llamacpp":
            return Applied({"chat_template_kwargs": {"reasoning_effort": value}}, note, True)
        return Applied({"reasoning_effort": value}, note, True)
    # kind == "toggle": Qwen3-style switch, with a budget on llama.cpp
    if backend == "ollama":
        return Applied({"think": on}, applied=True)
    if backend == "llamacpp":
        fields: dict[str, object] = {"chat_template_kwargs": {"enable_thinking": on}}
        fields["reasoning_budget"] = LLAMACPP_BUDGETS[level]
        return Applied(fields, applied=True)
    if backend == "openai-cloud":
        return Applied({}, "this cloud model has no thinking switch; effort ignored")
    return Applied({"chat_template_kwargs": {"enable_thinking": on}}, applied=True)

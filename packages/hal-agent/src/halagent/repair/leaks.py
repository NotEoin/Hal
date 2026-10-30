"""Leak parsers: tool calls a model wrote into its visible text instead of the tool-call field.

Each parser takes the content and returns `(calls, text with the calls cut out)`. They are
linear-time scanners or regexes without nested quantifiers, and every input is capped, because a
vLLM parser hung on malformed arrays in fuzzed input (design 03-agent, 3.6.2). A parser never
raises: unreadable text yields no calls. Name resolution and the offered-tool check happen in
`halagent.repair`, so a leaked call can never reach a tool the model was not given.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

MAX_INPUT_CHARS = 200_000
GEMMA_QUOTE = '<|"|>'
MAX_DEPTH = 20


@dataclass(frozen=True)
class RawCall:
    """A call found in text: the name and an argument dict (or raw text still to parse)."""

    name: str
    args: dict | str


Parser = Callable[[str], tuple[list[RawCall], str]]


def _json_obj(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _call_from_obj(obj: object) -> RawCall | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    args = obj.get("arguments", obj.get("parameters", {}))
    if not isinstance(name, str):
        return None
    return RawCall(name, args if isinstance(args, dict | str) else {})


class _GemmaValues:
    """Recursive-descent parser for Gemma's argument grammar (bare keys, `<|"|>` strings)."""

    def __init__(self, text: str) -> None:
        self.s, self.i = text, 0

    def _ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \n\t\r":
            self.i += 1

    def object_body(self, end: str, depth: int = 0) -> dict:
        out: dict = {}
        while True:
            self._ws()
            if self.i >= len(self.s) or self.s[self.i] == end:
                return out
            m = re.compile(r"[\w.\-]+").match(self.s, self.i)
            if not m:
                raise ValueError("expected a key")
            self.i = m.end()
            self._ws()
            if self.s[self.i : self.i + 1] != ":":
                raise ValueError("expected ':'")
            self.i += 1
            out[m.group()] = self.value(depth + 1)
            self._ws()
            if self.s[self.i : self.i + 1] == ",":
                self.i += 1

    def value(self, depth: int = 0) -> object:
        if depth > MAX_DEPTH:
            raise ValueError("too deep")
        self._ws()
        s = self.s
        if s.startswith(GEMMA_QUOTE, self.i):
            start = self.i + len(GEMMA_QUOTE)
            end = s.find(GEMMA_QUOTE, start)
            if end < 0:
                raise ValueError("unterminated string")
            self.i = end + len(GEMMA_QUOTE)
            return s[start:end]
        if s[self.i : self.i + 1] == "{":
            self.i += 1
            obj = self.object_body("}", depth)
            self.i += 1
            return obj
        if s[self.i : self.i + 1] == "[":
            self.i += 1
            items: list = []
            while True:
                self._ws()
                if self.i >= len(s):
                    raise ValueError("unterminated list")
                if s[self.i] == "]":
                    self.i += 1
                    return items
                items.append(self.value(depth + 1))
                self._ws()
                if s[self.i : self.i + 1] == ",":
                    self.i += 1
        m = re.compile(r"[^,}\]\s]+").match(s, self.i)
        if not m:
            raise ValueError("expected a value")
        self.i = m.end()
        word = m.group()
        if word in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[word]
        try:
            return int(word) if re.fullmatch(r"-?\d+", word) else float(word)
        except ValueError:
            return word


def parse_gemma_args(text: str) -> dict:
    """Parses `key:<|"|>v<|"|>,n:3` into a dict; raises `ValueError` on malformed input."""
    return _GemmaValues(text).object_body("\0")


def _sub(rx: re.Pattern[str], text: str, make: Callable[[re.Match[str]], list[RawCall]]):
    calls: list[RawCall] = []
    for m in rx.finditer(text):
        calls += make(m)
    return calls, rx.sub("", text).strip() if calls else text


GEMMA_RE = re.compile(r"<\|tool_call>call:(?P<name>[\w.\-]+)\{(?P<args>.*?)\}<tool_call\|>", re.S)
HERMES_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
QWEN_RE = re.compile(
    r"<tool_call>\s*<function=(?P<name>[\w.\-]+)>(?P<body>.*?)</function>\s*</tool_call>", re.S
)
QWEN_PARAM_RE = re.compile(r"<parameter=(?P<k>\w+)>\n?(?P<v>.*?)\n?</parameter>", re.S)
MISTRAL_RE = re.compile(
    r"\[TOOL_CALLS\](?P<name>[\w.\-]+)\[ARGS\](?P<args>\{.*?\})(?=\[TOOL_CALLS\]|\Z)", re.S
)
MISTRAL_LIST_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*\])", re.S)
HARMONY_RE = re.compile(
    r"(?:<\|start\|>assistant)?<\|channel\|>commentary to=(?:functions\.)?(?P<name>[\w.\-]+)\s*"
    r"(?:<\|constrain\|>json)?<\|message\|>(?P<args>\{.*?\})(?:<\|call\|>|$)",
    re.S,
)
GLM_RE = re.compile(
    r"<tool_call>(?P<name>[\w.\-]+)\s*(?P<body>(?:<arg_key>.*?</arg_key>\s*<arg_value>.*?</arg_value>\s*)*)</tool_call>",
    re.S,
)
GLM_ARG_RE = re.compile(r"<arg_key>(?P<k>.*?)</arg_key>\s*<arg_value>(?P<v>.*?)</arg_value>", re.S)
NAME_JSON_RE = re.compile(r"^\s*(?:\[TOOL_CALLS\])?\s*(?P<name>[a-z_][\w.\-]*)\s*(?P<args>\{.*\})\s*$", re.S)
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _gemma(text: str):
    def make(m: re.Match[str]) -> list[RawCall]:
        try:
            return [RawCall(m["name"], parse_gemma_args(m["args"]))]
        except (ValueError, IndexError):
            return []

    return _sub(GEMMA_RE, text, make)


def _hermes(text: str):
    return _sub(HERMES_RE, text, lambda m: [c] if (c := _call_from_obj(_json_obj(m[1]))) else [])


def _qwen_xml(text: str):
    def make(m: re.Match[str]) -> list[RawCall]:
        return [RawCall(m["name"], {p["k"]: p["v"] for p in QWEN_PARAM_RE.finditer(m["body"])})]

    return _sub(QWEN_RE, text, make)


def _mistral(text: str):
    calls, rest = _sub(MISTRAL_RE, text, lambda m: [RawCall(m["name"], m["args"])])
    if calls:
        return calls, rest
    m = MISTRAL_LIST_RE.search(text)
    if not m:
        return [], text
    try:
        items = json.loads(m[1])
    except ValueError:
        return [], text
    found = [c for c in (_call_from_obj(i) for i in items if isinstance(items, list)) if c]
    return found, (text[: m.start()] + text[m.end() :]).strip() if found else text


def _harmony(text: str):
    return _sub(HARMONY_RE, text, lambda m: [RawCall(m["name"], m["args"])])


def _glm(text: str):
    def make(m: re.Match[str]) -> list[RawCall]:
        return [RawCall(m["name"], {a["k"].strip(): a["v"].strip() for a in GLM_ARG_RE.finditer(m["body"])})]

    return _sub(GLM_RE, text, make)


def _name_json(text: str):
    m = NAME_JSON_RE.match(text)
    if not m:
        return [], text
    return [RawCall(m["name"], m["args"])], ""


def _json(text: str):
    fenced = FENCE_RE.match(text)
    body = (fenced[1] if fenced else text).strip()
    if not body or body[0] not in "{[":
        return [], text
    try:
        value = json.loads(body)
    except ValueError:
        return [], text
    items = value if isinstance(value, list) else [value]
    calls = [c for c in map(_call_from_obj, items) if c]
    return (calls, "") if calls and len(calls) == len(items) else ([], text)


PARSERS: dict[str, Parser] = {
    "gemma4": _gemma,
    "hermes": _hermes,
    "qwen_xml": _qwen_xml,
    "mistral": _mistral,
    "harmony": _harmony,
    "glm": _glm,
    "name_json": _name_json,
    "json": _json,
}
DEFAULT_ORDER = tuple(PARSERS)

OPEN_MARKERS = (
    (re.compile(r"<\|tool_call>(?!.*<tool_call\|>)", re.S)),
    (re.compile(r"<tool_call>(?!.*</tool_call>)", re.S)),
    (re.compile(r"\[TOOL_CALLS\][^{]*\{(?!.*\})", re.S)),
    (re.compile(r"to=functions\.(?!.*<\|call\|>)(?=[^}]*$)", re.S)),
)


def has_open_marker(text: str) -> bool:
    """True when the text ends inside an unclosed tool-call marker (a length-truncated call)."""
    text = text[:MAX_INPUT_CHARS]
    return any(rx.search(text) for rx in OPEN_MARKERS)


def parse_leaks(
    text: str,
    order: tuple[str, ...] = DEFAULT_ORDER,
    accept: Callable[[RawCall], bool] = lambda call: True,
) -> tuple[str, list[RawCall], str]:
    """Runs the parsers in order; the first with a call `accept` takes wins.

    Returns (parser name, calls, text with the calls removed).
    """
    text = text[:MAX_INPUT_CHARS]
    for name in order:
        calls, rest = PARSERS[name](text)
        if any(accept(c) for c in calls):
            return name, calls, rest
    return "", [], text

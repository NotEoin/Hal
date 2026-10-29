"""Argument normalisation before policy: declared aliases, coercion, bounds, lists and JSON.

The hub normalises; 01-security's `decide` validates (D21). This module handles only what the
hub knows: declared aliases, the CLI's string inputs, numeric bounds, list syntax and
`format = "json"`. Names, types after coercion, `enum`, `max_length`, required parameters
and NUL are 01's checks, so one validator of record decides (04-hub.md, "Arguments").

Design decisions:

- Keys starting with `_` are set aside and passed on untouched, so `decide` strips and
  audits them as self-approval attempts (REQ-HUB-16). The hub never reads them.
- A name that matches nothing is passed on unchanged, so `decide` rejects it naming the
  accepted parameters.
- Near-miss names (`when` for `when_text`) are mapped only for the CLI front ends, and only
  with exactly one candidate that is not also present (REQ-HUB-15, RESOLUTIONS B7): model
  front ends must not get argument repair from the hub (01 REQ-SEC-05).
- Every normalised value is a flat `str`, `int`, `float` or `bool`, as 01's canonical form
  needs. A list stays one comma-joined string until `call_arguments` splits it, after the
  decision and outside the approval key.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from halkit.canon import canonical_json

from .manifest import ParamSpec, ToolSpec

log = logging.getLogger(__name__)

MAX_ARGS_BYTES = 2**20  # 1 MiB of canonical JSON: one call cannot fill the audit log or a worker pipe
INTEGER_RE = re.compile(r"^[+-]?[0-9]+$")
TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
FALSE_WORDS = frozenset({"false", "no", "off", "0"})
LIST_SEPARATOR = ","


class ArgumentError(ValueError):
    """Arguments the hub cannot normalise; becomes `invalid_arguments` with rule `bad-args:<code>`."""

    def __init__(self, tool: str, param: str | None, message: str, *, code: str) -> None:
        self.tool = tool
        self.param = param
        self.message = message
        self.code = code
        where = f"{tool}.{param}" if param else tool
        super().__init__(f"{where}: {message}")


@dataclass(frozen=True)
class Normalised:
    """The arguments ready for 01's `prepare` and `decide`."""

    args: dict[str, object]  # coerced declared values plus unmatched and "_" keys
    aliased: tuple[tuple[str, str], ...]  # (given, mapped)
    warnings: tuple[str, ...]


def normalise(tool: ToolSpec, raw: Mapping[str, object] | None, *, near_miss: bool = False) -> Normalised:
    """Maps names and coerces values per 04-hub "Coercion"; raises `ArgumentError`.

    `near_miss` is True only for the `hal-hub` CLI and the pack CLI.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ArgumentError(
            tool.name, None, f"arguments must be an object, got {type(raw).__name__}", code="shape"
        )
    by_name = {p.name: p for p in tool.params}
    by_alias = {alias: p.name for p in tool.params for alias in p.aliases}
    given = {str(k) for k in raw}
    out: dict[str, object] = {}
    aliased: list[tuple[str, str]] = []
    warnings: list[str] = []

    for key, value in raw.items():
        key = str(key)
        if key.startswith("_"):
            out[key] = value  # for 01's decide to strip and audit (REQ-HUB-16)
            continue
        name = key
        if key not in by_name:
            if key in by_alias:
                name = by_alias[key]
            elif near_miss:
                name = _near_miss(key, by_name, given) or key
            if name != key:
                aliased.append((key, name))
                if key not in by_alias:
                    note = f"argument {key!r} read as {name!r}"
                    log.info("%s: %s", tool.name, note)
                    warnings.append(note)
        if name in out:
            raise ArgumentError(
                tool.name, name, f"given twice (as {key!r} and another name)", code="duplicate"
            )
        param = by_name.get(name)
        out[name] = value if param is None else _coerce(tool.name, param, value, warnings)

    size = len(canonical_json(out))
    if size > MAX_ARGS_BYTES:
        raise ArgumentError(
            tool.name, None, f"arguments are {size} bytes of JSON, the limit is {MAX_ARGS_BYTES}", code="size"
        )
    return Normalised(out, tuple(aliased), tuple(warnings))


def _near_miss(name: str, by_name: Mapping[str, ParamSpec], given: set[str]) -> str | None:
    """The one declared parameter `name` is a near miss for, unless it is ambiguous or present (v1 rule)."""
    flat = name.replace("_", "").lower()
    hits = [
        p
        for p in by_name
        if p.replace("_", "").lower() == flat or p.startswith(name + "_") or name.startswith(p + "_")
    ]
    if len(hits) == 1 and hits[0] not in given:
        return hits[0]
    return None


def _coerce(tool: str, p: ParamSpec, value: object, warnings: list[str]) -> object:
    def fail(message: str, code: str = "type") -> ArgumentError:
        return ArgumentError(tool, p.name, message, code=code)

    if p.list:
        result: object = _coerce_list(p, value, fail, tool)
    elif p.format == "json":
        result = _coerce_json(p, value, fail)
    elif p.type == "string":
        result = _coerce_string(value, fail)
    elif p.type == "integer":
        result = _coerce_integer(value, fail)
    elif p.type == "number":
        result = _coerce_number(value, fail)
    else:
        result = _coerce_boolean(value, fail)

    if isinstance(result, int | float) and not isinstance(result, bool):
        if p.minimum is not None and result < p.minimum:
            raise fail(f"{result} is below the minimum {p.minimum:g}", "range")
        if p.maximum is not None and result > p.maximum:
            raise fail(f"{result} is above the maximum {p.maximum:g}", "range")
    if p.enum is not None and result not in p.enum and isinstance(result, str):
        matches = [e for e in p.enum if isinstance(e, str) and e.lower() == result.lower()]
        if len(matches) == 1:
            note = f"argument {p.name!r}: {result!r} read as {matches[0]!r}"
            log.info("%s: %s", tool, note)
            warnings.append(note)
            result = matches[0]
    return result


def _coerce_string(value: object, fail) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    raise fail(f"expected a string, got {_kind(value)}")


def _coerce_integer(value: object, fail) -> int:
    if isinstance(value, bool):
        raise fail("expected an integer, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and INTEGER_RE.match(value.strip()):
        return int(value.strip())
    raise fail(f"expected an integer, got {_kind(value)} {_show(value)}")


def _coerce_number(value: object, fail) -> float:
    if isinstance(value, bool):
        raise fail("expected a number, got a boolean")
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise fail(f"expected a number, got {_show(value)}") from None
    else:
        raise fail(f"expected a number, got {_kind(value)}")
    if not math.isfinite(number):
        raise fail("expected a finite number")
    return number


def _coerce_boolean(value: object, fail) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
    words = ", ".join(sorted(TRUE_WORDS | FALSE_WORDS))
    raise fail(f"expected a boolean ({words}), got {_show(value)}")


def _coerce_list(p: ParamSpec, value: object, fail, tool: str) -> str:
    if isinstance(value, list):
        items = []
        for index, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, str | int) or LIST_SEPARATOR in str(item):
                raise fail(f"list item {index} must be a scalar without commas", "list")
            items.append(str(item).strip())
        log.info("%s: argument %r given as a JSON array; joined with commas", tool, p.name)
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(LIST_SEPARATOR)]
    else:
        raise fail(f"expected a comma-separated string, got {_kind(value)}", "list")
    items = [item for item in items if item]
    if p.item_type == "integer":
        for item in items:
            if not INTEGER_RE.match(item):
                raise fail(f"list item {item!r} is not an integer", "list")
        items = [str(int(item)) for item in items]
    return LIST_SEPARATOR.join(items)


def _coerce_json(p: ParamSpec, value: object, fail) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise fail(f"not valid JSON: {exc}", "json") from None
    elif p.v1_object and isinstance(value, dict | list):
        parsed = value
    else:
        raise fail(f"expected a string holding JSON, got {_kind(value)}", "json")
    try:
        return canonical_json(parsed).decode("ascii")
    except ValueError as exc:
        raise fail(f"JSON cannot be encoded canonically: {exc}", "json") from None


def _kind(value: object) -> str:
    return {bool: "a boolean", int: "an integer", float: "a number", str: "a string", list: "a list"}.get(
        type(value), f"a {type(value).__name__}"
    )


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def call_arguments(tool: ToolSpec, args: Mapping[str, object]) -> dict[str, object]:
    """What the function receives: lists split by `item_type`, v1 objects parsed; nothing else changes."""
    out: dict[str, object] = {}
    for name, value in args.items():
        p = tool.param(name)
        if p is not None and p.list and isinstance(value, str):
            items = [item for item in value.split(LIST_SEPARATOR) if item]
            out[name] = [int(item) for item in items] if p.item_type == "integer" else items
        elif p is not None and p.v1_object and isinstance(value, str):
            out[name] = json.loads(value)
        else:
            out[name] = value
    return out

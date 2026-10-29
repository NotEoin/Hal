"""Canonical JSON encoding, the input to approval keys and routine step hashes.

The rules are 01-security's "Approval binding": sorted keys, no whitespace, ASCII escapes,
finite numbers only, `true`/`false` for booleans, and strings exactly as given. Strings are
never Unicode-normalised, because normalisation would let one approval cover a visually
different string (REQ-SEC-20).

The encoder checks the value's shape before encoding instead of relying on `json.dumps`
defaults: `json.dumps` turns an integer key into a string and a tuple into a list silently, so
`{1: "a"}` and `{"1": "a"}` would share a key. Anything that is not a JSON value raises
`CanonicalJSONError`.
"""

from __future__ import annotations

import json
import math

MAX_DEPTH = 32  # deeper values are not tool arguments or step lists; matches REQ-SEC-65's nesting cap


class CanonicalJSONError(ValueError):
    """The value is not a JSON value with string keys and finite numbers."""


def canonical_json(value: object) -> bytes:
    """`value` as canonical JSON bytes: objects, lists (routine steps) and scalars.

    Raises `CanonicalJSONError` for a non-string key, a non-finite float, a tuple, a set or
    any other non-JSON type, and for nesting deeper than `MAX_DEPTH`.
    """
    _check(value, "$", 0)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _check(value: object, where: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise CanonicalJSONError(f"{where}: nested deeper than {MAX_DEPTH} levels")
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalJSONError(f"{where}: {value} is not a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check(item, f"{where}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(f"{where}: key {key!r} is not a string")
            _check(item, f"{where}.{key}", depth + 1)
        return
    raise CanonicalJSONError(f"{where}: {type(value).__name__} is not a JSON value")

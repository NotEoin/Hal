"""The `result._meta.hal` object on every broker `tools/call` result (REQ-BOX-67, RESOLUTIONS E7).

The agent in the box reads it to tell a failed tool from a denied one and to know whether the text
is untrusted. Decisions:

- Only four statuses exist. There is no approval id and no pending status: an `ask` decision
  blocks until a host surface answers, so the agent never learns an approval is in flight (B5).
- `guard` is null exactly when `untrusted` is false, so a reader cannot mistake "no scan" for "clean".
- The text content is never touched here: untrusted results already carry 01-security's envelope.
"""

from __future__ import annotations

from typing import Any

STATUSES = ("ok", "error", "denied", "unavailable")
GUARDS = ("clean", "flagged", "error", "off")


def build_meta(status: str, *, untrusted: bool = False, guard: str | None = None) -> dict[str, Any]:
    """The `hal` object; raises `ValueError` for an unknown status or an inconsistent guard."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; use one of {STATUSES}")
    if untrusted and guard not in GUARDS:
        raise ValueError(f"untrusted results need a guard value from {GUARDS}, got {guard!r}")
    if not untrusted and guard is not None:
        raise ValueError("guard must be None when the result is not untrusted")
    return {"status": status, "untrusted": untrusted, "guard": guard if untrusted else None}


def attach(
    result: dict[str, Any], status: str, *, untrusted: bool = False, guard: str | None = None
) -> dict[str, Any]:
    """Sets `isError` and `_meta.hal` on a `tools/call` result and returns it.

    Any `_meta.hal` the upstream sent is replaced, so a tool cannot forge its own status.
    `isError` is false only for `ok`.
    """
    meta = build_meta(status, untrusted=untrusted, guard=guard)
    result["isError"] = status != "ok"
    existing = result.get("_meta")
    result["_meta"] = {**(existing if isinstance(existing, dict) else {}), "hal": meta}
    return result

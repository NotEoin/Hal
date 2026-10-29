"""Phase-1 stand-in for the types of 01-security's `halhub.policy` that the hub builds.

04-hub's implementation plan lands the manifest, registry, argument and runner steps before
01-security's policy engine, testing them "against their published interfaces with fakes".
This module copies only the published shapes the hub constructs (`Tier`, `ParamSpec` and
`ToolMeta`, from 01-security.md, "Decision algorithm") with the same names, fields and
defaults, so switching to `halhub.policy` is an import change.

What is missing on purpose: `prepare`, `decide`, `Policy`, grants and everything else that
makes a decision. Nothing here decides anything. This module is deleted when 01-security's
`halhub.policy` lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Tier(StrEnum):
    """A tool's tier; what each one means for policy is 01-security's tier table."""

    READ = "read"
    WRITE = "write"
    ACT = "act"
    SEND = "send"
    SYSTEM = "system"


@dataclass(frozen=True)
class ParamSpec:
    """The parameter facts 01's `decide` validates (type, enum, length, format, access)."""

    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = False
    enum: tuple[object, ...] | None = None
    max_length: int = 65536
    format: Literal["text", "path", "url", "host", "email", "phone", "command", "json"] = "text"
    access: Literal["read", "write"] = "read"
    content: bool = False


@dataclass(frozen=True)
class ToolMeta:
    """Everything 01's policy reads about one tool; built by `ToolSpec.meta`."""

    name: str
    pack: str
    pack_version: str
    tier: Tier
    params: tuple[ParamSpec, ...]
    network: bool = False
    untrusted_output: bool = False
    reviewable: bool = False
    reversible: bool = False
    runs_code: bool = False
    domains: tuple[str, ...] = ()

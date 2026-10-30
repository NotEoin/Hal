"""Small value types shared by the hal-remote modules (design 07, Python interfaces).

Only the types a finished step needs live here; the profile types arrive with the step that
reads them. Client names are checked in one place (`check_client_name`) because the name ends
up in an `authorized_keys` comment and in a forced command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Role = Literal["tunnel", "mcp"]
ROLES: tuple[Role, ...] = ("tunnel", "mcp")
CLIENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")  # design 07, Authorize request


def check_client_name(name: str) -> str:
    """Returns `name` when it is a valid client name, else raises `ValueError`."""
    if not CLIENT_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid client name {name!r}: use 1 to 32 characters of a-z, 0-9 and -")
    return name


@dataclass(frozen=True)
class Forward:
    """One local forward of the tunnel; the gateway is the only one in v1 (Decision D13)."""

    name: Literal["gateway"]
    local_port: int
    remote_port: int

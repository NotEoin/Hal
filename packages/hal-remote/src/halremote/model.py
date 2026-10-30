"""Small value types shared by the hal-remote modules (design 07, Python interfaces).

Client names are checked in one place (`check_client_name`) because the name ends
up in an `authorized_keys` comment and in a forced command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from halkit import paths

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


@dataclass(frozen=True)
class TunnelSettings:
    """Per-profile `[tunnel]` keys; the defaults and their reasons are in design 07."""

    server_alive_interval_s: int = 15  # with count 3, a dead link is detected in about 45 s
    server_alive_count_max: int = 3  # the OpenSSH default; tolerates brief Wi-Fi loss
    connect_timeout_s: int = 10  # a LAN host answers well under 1 s; 10 s allows a slow wake
    backoff_initial_s: int = 2
    backoff_max_s: int = 60
    stable_after_s: int = 60
    health_interval_s: int = 30


@dataclass(frozen=True)
class Profile:
    """One paired desktop, as stored in `profile.toml`."""

    name: str
    client: str
    host: str
    user: str
    port: int
    forwards: tuple[Forward, ...]
    tunnel: TunnelSettings = field(default_factory=TunnelSettings)
    created: str = ""  # UTC ISO 8601 with Z; shown in the generated ssh_config header


@dataclass(frozen=True)
class ProfilePaths:
    """Where a profile keeps its files (config side `root`, state side `state`)."""

    root: Path
    ssh_config: Path
    known_hosts: Path
    tunnel_key: Path
    mcp_key: Path
    token: Path
    state: Path


def profile_paths(name: str) -> ProfilePaths:
    """The file layout of profile `name` under `config_dir()` and `state_dir()`."""
    check_client_name(name)  # the same shape rule keeps the name safe in paths and ssh aliases
    root = paths.config_dir() / "remote" / name
    return ProfilePaths(
        root=root,
        ssh_config=root / "ssh_config",
        known_hosts=root / "known_hosts",
        tunnel_key=root / "tunnel_ed25519",
        mcp_key=root / "mcp_ed25519",
        token=root / "gateway-token",
        state=paths.state_dir() / "remote" / name,
    )


@dataclass(frozen=True)
class ClientRecord:
    """One authorized laptop, as stored in the desktop registry `clients.toml`."""

    name: str
    created: str
    updated: str
    from_pattern: str  # a `from=` pattern, or "any" when the option is omitted
    expires: str  # YYYY-MM-DD, empty for none
    tunnel_fingerprint: str
    mcp_fingerprint: str
    forwards: tuple[int, ...]
    gateway_token_name: str
    accepted_risks: tuple[str, ...] = ()

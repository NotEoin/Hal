"""The `[remote]` and `[remote.server]` config tables of hal-remote (design 07, Configuration keys).

Registering at import time lets `hal configure --show` list the keys without importing the
rest of the package. Per-profile tunnel keys live in each profile's `profile.toml`, not here.
"""

from __future__ import annotations

from halkit import config

OWNER = "hal-remote"
DEFAULT_MAX_SESSIONS = 4  # one or two harnesses per laptop is normal; the cap bounds a looping client
DEFAULT_DELIVERY_TTL_S = 86400  # a day covers the manual flow across two machines
DEFAULT_FROM_PREFIX_V4 = 24  # home LANs are /24; DHCP changes only the host part
DEFAULT_FROM_PREFIX_V6 = 64  # SLAAC and privacy addresses stay inside the /64
MAX_PREFIX_V4 = 32
MAX_PREFIX_V6 = 128


def _prefix(limit: int):
    def check(value: object) -> str | None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= limit:
            return f"must be an integer from 1 to {limit}"
        return None

    return check


def _positive(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return "must be a positive integer"
    return None


K = config.Key

REMOTE_TABLE = config.register(
    config.Table(
        name="remote",
        owner=OWNER,
        keys=(
            K("default_profile", str, "", doc="Profile used when none is named; empty = the only one"),
            K("ssh", str, "", doc="Path of the OpenSSH client; empty = discover"),
            K("desktop_tools", bool, True, doc="Laptop `hal code` gets the desktop's tools"),
        ),
    )
)

SERVER_TABLE = config.register(
    config.Table(
        name="remote.server",
        owner=OWNER,
        keys=(
            K(
                "max_sessions",
                int,
                DEFAULT_MAX_SESSIONS,
                check=_positive,
                doc="Concurrent sessions per client",
            ),
            K(
                "delivery_ttl_s",
                int,
                DEFAULT_DELIVERY_TTL_S,
                check=_positive,
                doc="Pending token delivery lifetime",
            ),
            K(
                "from_prefix_v4",
                int,
                DEFAULT_FROM_PREFIX_V4,
                check=_prefix(MAX_PREFIX_V4),
                doc="IPv4 from= prefix",
            ),
            K(
                "from_prefix_v6",
                int,
                DEFAULT_FROM_PREFIX_V6,
                check=_prefix(MAX_PREFIX_V6),
                doc="IPv6 from= prefix",
            ),
            K("serve_path", str, "", doc="Absolute path of hal-remote for command=; empty = resolve"),
            K("hub_path", str, "", doc="Absolute path of hal-hub; empty = resolve"),
            K("hal_path", str, "", doc="Absolute path of hal; empty = resolve"),
        ),
    )
)

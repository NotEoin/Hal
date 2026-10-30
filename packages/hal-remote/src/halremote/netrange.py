"""Derives and validates the `from=` pattern of an `authorized_keys` line (REQ-RMT-06).

The default is the narrowest range that survives DHCP and SLAAC address changes. A public
IPv4 source or an IPv6 link-local one is refused: the first means the desktop is exposed
beyond the LAN, the second cannot be written into `from=` (scope ids do not survive).
"""

from __future__ import annotations

import ipaddress
import re

DEFAULT_V4_PREFIX = 24
DEFAULT_V6_PREFIX = 64
V4_HOST_PREFIX = 32
V6_HOST_PREFIX = 128
ANY = "any"
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and carrier-grade NAT
PRIVATE_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
ULA_V6 = ipaddress.ip_network("fc00::/7")
_PATTERN_RE = re.compile(r"^!?[0-9a-fA-F.:*?]+$")  # sshd pattern items, per design 07 (from= derivation)


class FromPatternError(ValueError):
    """An address cannot be turned into a safe `from=` pattern, or a pattern is invalid."""


def _network(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, prefix: int) -> str:
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def derive(addr: str, *, v4_prefix: int = DEFAULT_V4_PREFIX, v6_prefix: int = DEFAULT_V6_PREFIX) -> str:
    """Returns the `from=` pattern for a client address; raises `FromPatternError` if refused."""
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        raise FromPatternError(f"{addr!r} is not an IP address") from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # sshd reports mapped addresses on dual-stack sockets
    if ip.is_loopback:
        return str(ip)
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in CGNAT:
            return f"{ip}/{V4_HOST_PREFIX}"
        if any(ip in net for net in PRIVATE_V4):
            return _network(ip, v4_prefix)
        raise FromPatternError(
            f"{ip} is a public address: the desktop would be reachable beyond the LAN; "
            "pass --from with the range you intend"
        )
    if ip.is_link_local:
        raise FromPatternError(f"{ip} is link-local; scope ids do not survive in from=, pass --from")
    if ip in ULA_V6 or ip.is_global:
        return _network(ip, v6_prefix)
    raise FromPatternError(f"{ip} is not a unicast address that can be matched; pass --from")


def validate(pattern: str) -> str:
    """Checks a `--from` value (comma-separated CIDRs or sshd patterns) and returns it normalised.

    `any` is returned as is; the caller omits the option and warns.
    """
    value = pattern.strip()
    if value == ANY:
        return ANY
    items = [i.strip() for i in value.split(",")]
    if not all(items):
        raise FromPatternError(f"empty item in from= value {pattern!r}")
    for item in items:
        if "/" in item:
            try:
                ipaddress.ip_network(item.removeprefix("!"), strict=False)
            except ValueError:
                raise FromPatternError(f"{item!r} is not a valid CIDR") from None
        elif not _PATTERN_RE.fullmatch(item):
            raise FromPatternError(f"{item!r} is not a valid sshd address pattern")
    return ",".join(items)

"""A scripted DNS resolver for `halkit.netguard.fetch`, so egress tests never touch DNS."""

from __future__ import annotations

from collections.abc import Mapping


class FakeResolver:
    """Maps hostnames to address lists and records every lookup in `lookups`.

    An unknown name raises `OSError`, like a failed lookup. Tests assert on `lookups` to prove
    that no model-chosen host was resolved before the policy allowed it (REQ-SEC-10).
    """

    def __init__(self, table: Mapping[str, list[str]]) -> None:
        self.table = {name.lower(): list(addresses) for name, addresses in table.items()}
        self.lookups: list[str] = []

    def __call__(self, host: str) -> list[str]:
        """Returns the scripted addresses of `host`."""
        self.lookups.append(host)
        try:
            return list(self.table[host.lower()])
        except KeyError:
            raise OSError(f"FakeResolver has no entry for {host}") from None

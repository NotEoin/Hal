"""Tests for `halhub.mcp.annotations`: MCP hints derived from the tier (REQ-HUB-39, 01 REQ-SEC-87).

The expected rows are 01-security.md, "MCP annotations", written out by hand so a change to
the code that drifts from the table fails here.
"""

from __future__ import annotations

import pytest

from halhub.mcp import annotations
from halhub.policy import Tier, ToolMeta


def meta(tier: str, *, network: bool = False, reversible: bool = False) -> ToolMeta:
    return ToolMeta("t_x", "p", "0.1.0", Tier(tier), (), network=network, reversible=reversible)


# (tier, network, reversible) -> (readOnly, destructive, idempotent, openWorld)
TABLE = [
    (("read", False, False), (True, False, True, False)),
    (("read", True, False), (True, False, True, True)),
    (("write", False, False), (False, False, False, False)),
    (("write", True, False), (False, False, False, True)),
    (("act", False, False), (False, True, False, False)),
    (("act", False, True), (False, False, False, False)),
    (("act", True, True), (False, False, False, True)),
    (("send", False, False), (False, True, False, True)),
    (("system", False, False), (False, True, False, False)),
    (("system", True, False), (False, True, False, True)),
]


@pytest.mark.parametrize(("row", "hints"), TABLE)
def test_annotations_follow_security_table(row, hints):
    tier, network, reversible = row
    got = annotations(meta(tier, network=network, reversible=reversible))
    names = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
    assert tuple(got[n] for n in names) == hints and set(got) == set(names)

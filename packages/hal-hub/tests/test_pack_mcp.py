"""Integration test: a fixture pack's `mcp` subcommand served over stdio (REQ-HUB-49).

Marked `slow`: it starts a Python process that loads the SDK. Hermetic: the Hal home is the
temporary one from `conftest.py` and the pack is the synthetic `demo` module.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.slow

SERVE_TIMEOUT_S = 60  # a cold start loads the SDK; far above the usual 2 s
PACK_MAIN = "import sys; from halhub import pack; sys.exit(pack.main('demo', ('demo',)))"


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_pack_mcp_lists_and_calls_only_its_own_tools(hal_home, demo_token, fixtures, mode):
    dirs = [str(fixtures / "demo"), str(fixtures / "routines")]
    with hal_home.config.open("a") as fh:
        fh.write(f"[hub]\nmodule_paths = {json.dumps(dirs)}\nuse_entry_points = false\n")
    params = StdioServerParameters(
        command=sys.executable, args=["-c", PACK_MAIN, "mcp"], env=dict(os.environ)
    )

    async def main():
        async with Client(params, mode=mode, read_timeout_seconds=SERVE_TIMEOUT_S) as client:
            listed = await client.list_tools()
            echoed = await client.call_tool("demo_echo", {"text": "yo"})
            other = await client.call_tool("routines_save_scene", {})
            return [t.name for t in listed.tools], echoed, other

    names, echoed, other = anyio.run(main)
    assert names == ["demo_echo", "demo_fetch", "demo_lights", "demo_wipe"]
    assert echoed.content[0].text == "yo" and not echoed.is_error
    assert json.loads(other.content[0].text)["error_type"] == "unknown_tool"

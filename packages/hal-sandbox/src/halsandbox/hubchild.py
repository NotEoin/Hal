"""The broker's per-connection hub process: `halhub mcp` plus the session's host MCP upstreams.

Run as `python -m halsandbox.hubchild UPSTREAMS.json -- <halhub mcp arguments>`. The JSON file
holds the `[mcp.servers]` table the broker was given. It is a separate module so the plain
broker command stays `halhub mcp` when a session has no upstreams.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from halhub import cli

from .upstreams import install, parse_upstreams


def main(argv: list[str] | None = None) -> int:
    """Installs the upstreams named by the first argument, then runs `halhub` with the rest."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[1] != "--":
        sys.stderr.write("usage: python -m halsandbox.hubchild UPSTREAMS.json -- <halhub arguments>\n")
        return 2
    servers = json.loads(Path(args[0]).read_text())
    install(parse_upstreams(servers.get("servers", {}), restart_limit=int(servers.get("restart_limit", 1))))
    return cli.main(args[2:])


if __name__ == "__main__":
    sys.exit(main())

"""A synthetic stdio MCP server for the upstream tests: `echo`, `boom` (raises), `die` (exits), `env`."""

import json
import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("stub")


@server.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return text


@server.tool()
def boom() -> str:
    """Raise, which MCP reports as an error result."""
    raise RuntimeError("synthetic failure")


@server.tool()
def die() -> str:
    """Exit the server process at once."""
    os._exit(3)


@server.tool()
def env() -> str:
    """The names of the environment variables the server was started with."""
    return json.dumps(sorted(os.environ))


if __name__ == "__main__":
    server.run("stdio")

"""Hal remote: the optional SSH add-on that lets a laptop (Linux or Windows) use Hal on the
desktop through a restricted key, a tunnel and MCP over SSH.

It imports only `halkit`, so a Windows laptop can install `hal-kit` and `hal-remote` without the
Linux-only sandbox. Design: docs/design/07-remote.md.
Importing this package registers the `[remote]` config tables.
"""

__version__ = "0.3.0"

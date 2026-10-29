"""Hal remote: the optional SSH add-on that lets a laptop (Linux or Windows) use Hal on the
desktop through a restricted key, a tunnel and MCP over SSH.

It imports only `halkit`, so a Windows laptop can install `hal-kit` and `hal-remote` without the
Linux-only sandbox. Design: docs/design/07-remote.md.
This package is a skeleton: the first behaviour lands in phase 7 of the implementation plan.
"""

__version__ = "0.1.0.dev0"

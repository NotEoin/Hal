"""The local prompt-injection guard: result type, scanner protocol, client and host relay.

01-security "Taint, guard and reviewer" (A4: in `halhub`, so `hal-hub mcp` applies it too).
`client.GuardClient` talks to the relay on `runtime_dir()/guard.sock`; `serve` is the relay
(`python -m halhub.guard.serve`) that starts the worker of `packages/hal-hub/guard-image/`.
Every failure is a result with label `error`, which the hub treats as a flag (REQ-SEC-35).
"""

from __future__ import annotations

from .client import GuardClient, GuardResult, GuardScanner, GuardUnavailable, socket_path

__all__ = ["GuardClient", "GuardResult", "GuardScanner", "GuardUnavailable", "socket_path"]

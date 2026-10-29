"""Test helpers for the hub's pipeline tests: a spy runner and a scripted approval surface.

Plain classes, imported by `conftest.py` and the test files (pytest puts this directory on
`sys.path`), so no test imports from `conftest`.
"""

from __future__ import annotations

from pathlib import Path

from halhub._approvals_standin import Decision
from halhub._policy_standin import Scope
from halhub.runner import RunOutcome

DEMO_TOKEN = "demo-token-canary-value"  # synthetic; never a real credential


class SpyRunner:
    """A runner that records each run and returns `result` (or `result(tool, args)`) without a process."""

    def __init__(self, result: object = "ok") -> None:
        self.result = result
        self.calls: list[tuple[str, dict, dict]] = []
        self.timeouts: list[float] = []

    def run(self, module, tool, args, *, timeout, secrets=None, cancel=None, on_tool_call=None):
        self.calls.append((tool.name, dict(args), dict(secrets or {})))
        self.timeouts.append(timeout)
        if isinstance(self.result, RunOutcome):
            return self.result
        value = self.result(tool, args) if callable(self.result) else self.result
        return RunOutcome(True, result=value)

    def close(self) -> None:
        pass


class ScriptedApprover:
    """An approval surface whose answer comes from `answer(request)`; records every request."""

    name = "scripted"
    shows_all_args = True

    def __init__(self, answer=None, *, available: bool = True) -> None:
        self._answer = answer or allow_once
        self._available = available
        self.requests: list = []

    def available(self) -> bool:
        return self._available

    def decide(self, request, timeout_s):
        self.requests.append(request)
        return self._answer(request)

    def withdraw(self, request_id: str) -> None:
        pass


def answer(verdict: str, scope=None, *, source: str = "scripted", key: str | None = None, note: str = ""):
    """An answer function for `ScriptedApprover`."""
    return lambda r: Decision(r.id, key or r.key, verdict, scope, source, note)


def allow_once(request):
    return Decision(request.id, request.key, "allow", Scope.ONCE, "scripted")


PROBE_KEY = "probe-key-canary-value"  # synthetic; never a real credential


def add_probe_key(config: Path, directory: Path) -> None:
    """Appends a `[probe]` table with a 0600 key file to `config`, so probe workers can start."""
    key = directory / "probe-key"
    key.write_text(PROBE_KEY + "\n")
    key.chmod(0o600)
    with config.open("a") as fh:
        fh.write(f'[probe]\nkey_file = "{key}"\n')

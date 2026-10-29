"""Test helpers for the hub's pipeline tests: a spy runner and scripted approval surfaces.

Plain classes, imported by `conftest.py` and the test files (pytest puts this directory on
`sys.path`), so no test imports from `conftest`.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from halhub import policy
from halhub.approvals import Decision
from halhub.policy import Scope
from halhub.runner import RunOutcome

DEMO_TOKEN = "demo-token-canary-value"  # synthetic; never a real credential


def short_approval_timeout(monkeypatch, seconds: float = 1.0) -> None:
    """Gives every policy the hub loads an approval timeout of `seconds`.

    The loader refuses `[approval].timeout_s` below 10 s (01 "`policy.toml` schema"), which
    is right for users and too slow for a test that waits for the timeout.
    """
    real = policy.load_policy

    def load(*args, **kwargs):
        loaded = real(*args, **kwargs)
        return replace(loaded, approval=replace(loaded.approval, timeout_s=seconds))

    monkeypatch.setattr(policy, "load_policy", load)


class FakeTerminal:
    """Stands in for a terminal on stdin; the tests never read from it."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("not a real terminal")


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


SILENT_CAP_S = 5.0  # a silent FakeSurface waits to be withdrawn, never past this


class FakeSurface:
    """01's `FakeSurface`: an `Approver` with a chosen `shows_all_args` that answers with a
    scripted answer function, or never (it then blocks until withdrawn).

    Uses `halhub.approvals.Decision`; `answer` functions build their own decisions.
    """

    def __init__(
        self, name: str, answer=None, *, shows_all_args: bool = True, available: bool = True
    ) -> None:
        self.name = name
        self.shows_all_args = shows_all_args
        self._answer = answer
        self._available = available
        self.seen: list = []
        self.withdrawn: list[str] = []
        self._gone = threading.Event()

    def available(self) -> bool:
        return self._available

    def decide(self, request, timeout_s: float):
        from halhub.approvals import Decision as RealDecision

        self.seen.append(request)
        if self._answer is None:
            self._gone.wait(SILENT_CAP_S)
            return RealDecision(request.id, request.key, "deny", None, "timeout")
        return self._answer(request)

    def withdraw(self, request_id: str) -> None:
        self.withdrawn.append(request_id)
        self._gone.set()


class FakePeer:
    """01's `FakePeer`: injected `SO_PEERCRED` values and `/proc/<pid>/environ` for control sockets.

    Pass `peer=fake.peer, environ=fake.environ` to `ControlServer`; `environ=None` makes the
    environment unreadable, like a process that already exited.
    """

    def __init__(self, *, uid: int | None = None, environ: bytes | None = b"PATH=/bin\0") -> None:
        import os

        self.pid, self.uid, self.gid = os.getpid(), os.getuid() if uid is None else uid, os.getgid()
        self._environ = environ

    def peer(self, conn):
        from halhub.approvals.control import Peer

        return Peer(self.pid, self.uid, self.gid)

    def environ(self, pid: int) -> bytes:
        if self._environ is None:
            raise FileNotFoundError(f"/proc/{pid}/environ")
        return self._environ


PROBE_KEY = "probe-key-canary-value"  # synthetic; never a real credential


def add_probe_key(config: Path, directory: Path) -> None:
    """Appends a `[probe]` table with a 0600 key file to `config`, so probe workers can start."""
    key = directory / "probe-key"
    key.write_text(PROBE_KEY + "\n")
    key.chmod(0o600)
    with config.open("a") as fh:
        fh.write(f'[probe]\nkey_file = "{key}"\n')


class FakeGuard:
    """A guard relay on a unix socket that answers each scan from a script (01 "Fakes").

    Each entry of `script` is used for one request, the last one for every request after it:
    `("score", 0.97)` replies with that score, `("error", "why")` with a worker error,
    `("hang", seconds)` waits that long before replying, `("garbage", "text")` sends a line
    that is not JSON, and `("other-id", score)` answers another request id.
    """

    def __init__(self, path: Path, script: list[tuple[str, object]]) -> None:
        import socket

        self.path = path
        self.script = list(script)
        self.requests: list[dict] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn) -> None:
        import json
        import time

        with conn:
            line = conn.makefile("rb").readline()
            if not line:
                return  # a connect-only probe
            request = json.loads(line)
            self.requests.append(request)
            kind, value = self.script[min(len(self.requests), len(self.script)) - 1]
            reply: object = {"v": 1, "id": request["id"], "windows": 1, "ms": 5, "model": "fake-guard"}
            if kind == "hang":
                time.sleep(float(value))  # type: ignore[arg-type]  # the script gives seconds
                kind, value = "score", 0.0
            if kind == "score":
                reply["score"] = value  # type: ignore[index]
            elif kind == "error":
                reply = {"v": 1, "id": request["id"], "error": value}
            elif kind == "other-id":
                reply.update(id="g_other", score=value)  # type: ignore[attr-defined]
            data = (str(value) if kind == "garbage" else json.dumps(reply)).encode() + b"\n"
            try:
                conn.sendall(data)
            except OSError:
                pass  # the client gave up first

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(1)

"""The guard client: one JSON line to the relay socket per scan, fail closed on any problem.

01-security "Taint, guard and reviewer", REQ-SEC-34 and REQ-SEC-35. The hub calls `scan` on
exactly the text it forwards for an `untrusted_output` tool, after secret scrubbing and output
capping.

Design decisions:

- `scan` never raises: a missing socket, a refused or slow connection, a malformed or
  mismatched reply, an `error` reply and a text over `max_scan_chars` all come back as label
  `error`, which the hub treats as a flag.
- The label is computed here from the worker's score and this session's `[guard].threshold`,
  so the threshold in `policy.toml` is the one that counts, whatever the worker was started
  with.
- One connection per scan, with `[guard].timeout_s` as the deadline for the whole exchange:
  the relay serialises requests to its one worker, so a pool would only queue.
"""

from __future__ import annotations

import json
import math
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from halkit import paths

from ..policy.model import GuardSettings

PROTOCOL_VERSION = 1
SOCKET_NAME = "guard.sock"
MAX_REPLY_BYTES = 64 * 1024  # a reply is a few hundred bytes; anything longer is not the worker
REQUEST_ID_BYTES = 6

GuardVerdict = Literal["benign", "injection", "error"]


@dataclass(frozen=True)
class GuardResult:
    """One scan's outcome; `label` `error` counts as a flag (REQ-SEC-35)."""

    label: GuardVerdict
    score: float | None
    windows: int
    ms: int
    model: str
    error: str | None = None


class GuardScanner(Protocol):
    """Anything that scans tool output for prompt injection: `GuardClient` or a test fake."""

    def scan(self, text: str, *, source_tool: str) -> GuardResult:
        """Returns the scan of `text`, never raising for a guard failure."""
        ...


class GuardUnavailable(RuntimeError):
    """The guard is enabled but cannot run; the message names the missing piece (REQ-SEC-35)."""


def socket_path() -> Path:
    """`runtime_dir()/guard.sock`, the relay's socket."""
    return paths.runtime_dir() / SOCKET_NAME


class GuardClient:
    """Scans text through the relay at `path` (default `socket_path()`) under `settings`."""

    def __init__(self, settings: GuardSettings, *, path: Path | None = None) -> None:
        self.settings = settings
        self.path = path

    def _path(self) -> Path:
        return self.path if self.path is not None else socket_path()

    def check(self) -> None:
        """Raises `GuardUnavailable` naming the problem when the relay cannot be reached."""
        try:
            path = self._path()
        except paths.RuntimeDirMissing as exc:
            raise GuardUnavailable(f"guard enabled but there is no runtime directory: {exc}") from exc
        if not path.exists():
            fix = "run `hal guard install` and start hal-guard.service"
            raise GuardUnavailable(f"guard enabled but {path} does not exist; {fix}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.settings.timeout_s)
                conn.connect(str(path))
        except OSError as exc:
            raise GuardUnavailable(f"guard enabled but {path} refuses connections: {exc}") from exc

    def scan(self, text: str, *, source_tool: str) -> GuardResult:
        """The scan of `text`; every failure is label `error`."""
        model = self.settings.model
        limit = self.settings.max_scan_chars
        if len(text) > limit:
            return _error(model, f"{len(text)} characters is over max_scan_chars {limit}")
        request_id = "g_" + secrets.token_hex(REQUEST_ID_BYTES)
        request = {"v": PROTOCOL_VERSION, "id": request_id, "text": text, "source_tool": source_tool}
        started = time.monotonic()
        try:
            line = self._exchange(json.dumps(request).encode("ascii") + b"\n", started)
            reply = json.loads(line)
        except (OSError, ValueError, paths.RuntimeDirMissing) as exc:
            return _error(model, f"{type(exc).__name__}: {exc}", started)
        return self._result(reply, request_id, started)

    def _exchange(self, payload: bytes, started: float) -> bytes:
        deadline = started + self.settings.timeout_s
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.settings.timeout_s)
            conn.connect(str(self._path()))
            conn.sendall(payload)
            buffer = b""
            while not buffer.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no reply within {self.settings.timeout_s} s")
                conn.settimeout(remaining)
                chunk = conn.recv(4096)
                if not chunk:
                    raise ConnectionError("the relay closed the connection without a reply")
                buffer += chunk
                if len(buffer) > MAX_REPLY_BYTES:
                    raise ValueError(f"reply is over {MAX_REPLY_BYTES} bytes")
        return buffer

    def _result(self, reply: object, request_id: str, started: float) -> GuardResult:
        model = self.settings.model
        if not isinstance(reply, dict) or reply.get("v") != PROTOCOL_VERSION or reply.get("id") != request_id:
            return _error(model, "reply does not answer this request", started)
        name = reply.get("model") if isinstance(reply.get("model"), str) else model
        if "error" in reply:
            return _error(str(name), f"guard worker: {reply['error']}", started)
        score, windows, ms = reply.get("score"), reply.get("windows"), reply.get("ms")
        valid_score = isinstance(score, int | float) and not isinstance(score, bool) and math.isfinite(score)
        if not valid_score or not 0 <= score <= 1 or not isinstance(windows, int) or not isinstance(ms, int):
            return _error(str(name), "reply has no valid score, windows and ms", started)
        label: GuardVerdict = "injection" if score >= self.settings.threshold else "benign"
        return GuardResult(label, float(score), windows, ms, str(name))


def _error(model: str, why: str, started: float | None = None) -> GuardResult:
    ms = round((time.monotonic() - started) * 1000) if started is not None else 0
    return GuardResult("error", None, 0, ms, model, error=why)

"""hal-hub worker: imports one tool module and runs its functions on request, one at a time.

The hub starts it as `<python> -P -X utf8 hal_worker.py --module M --functions a,b` and speaks
the worker protocol v1 over the original stdin and stdout: one ASCII JSON object per line
(04-hub.md, "Worker protocol (v1)"; `docs/dev/worker-protocol.md` once written).

Design decisions:

- Stdlib only, Python 3.11 or later, alone in its directory and run by path: a pack's own
  environment (an own-env venv, the system Python of the `desktop` pack) needs no `halhub`
  installed, and with `-P` no file next to this script can shadow an import (D6).
- The protocol runs on private duplicates of fd 0 and fd 1; fd 0 then reads `/dev/null`
  and fd 1 points at stderr, so a tool's `print` or `os.write(1, ...)` can never corrupt the
  protocol (REQ-HUB-26).
- Secrets arrive inside each `call` message and live in a per-call mapping that is cleared
  when the result has been sent (REQ-HUB-69); they never touch the environment or argv.
- Tracebacks go to stderr only; the hub gets the exception's class name and message (REQ-HUB-47).
- Tool code imports this file as `hal_worker` (for `secret`, `call_tool` and `ToolError`);
  it is registered in `sys.modules` under that name before the tool module is imported.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import ssl
import sys
import threading
import traceback

PROTOCOL_VERSION = 1
MAX_ERROR_CHARS = 2000  # an error message is for a person or a model, not a dump
SYSTEM_CA_FILE = "/etc/ssl/certs/ca-certificates.crt"  # Debian and Ubuntu bundle (hal-hub commit 1361783)


class ToolError(Exception):
    """Raise from a tool to report a plain failure; the hub shows only the message."""


class ToolCallError(Exception):
    """A nested `call_tool` was refused or failed; `type` is the hub's error type."""

    def __init__(self, error_type: str, message: str) -> None:
        self.type = error_type
        self.message = message
        super().__init__(f"{error_type}: {message}")


class _Channel:
    """The protocol stream: private file objects on duplicates of the original fd 0 and fd 1."""

    def __init__(self) -> None:
        self._reader = os.fdopen(os.dup(0), "rb")
        self._out = os.fdopen(os.dup(1), "wb", buffering=0)  # unbuffered: every line leaves at once
        self._write_lock = threading.Lock()
        self.read_lock = threading.Lock()
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        os.dup2(2, 1)
        sys.stdout = sys.stderr

    def send(self, message: dict) -> None:
        line = json.dumps({"v": PROTOCOL_VERSION, **message}, ensure_ascii=True, allow_nan=False) + "\n"
        with self._write_lock:
            self._out.write(line.encode("ascii"))

    def receive(self) -> dict | None:
        """The next message, or None at end of input; raises `ValueError` for a malformed line."""
        line = self._reader.readline()
        if not line:
            return None
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("a protocol message must be a JSON object")
        return message


_channel: _Channel | None = None
_current_call: str | None = None
_secrets: dict[str, str] = {}
_nested_counter = 0


def secret(name: str) -> str:
    """The value of secret `name` for the running call; raises `KeyError` when not declared or not set."""
    if name not in _secrets:
        raise KeyError(f"secret {name!r} is not declared by this module or not set")
    return _secrets[name]


def call_tool(tool: str, args: dict | None = None) -> object:
    """Calls another hub tool from inside a running call (only tools the manifest lists in `calls`).

    Returns the tool's result; raises `ToolCallError` when the hub refuses or the tool fails.
    """
    global _nested_counter
    if _channel is None or _current_call is None:
        raise ToolCallError("not_in_call", "call_tool works only while the hub is running a call")
    with _channel.read_lock:
        _nested_counter += 1
        nested_id = f"n-{_nested_counter:06d}"
        _channel.send(
            {"op": "tool_call", "id": nested_id, "parent": _current_call, "tool": tool, "args": args or {}}
        )
        while True:
            message = _channel.receive()
            if message is None:
                raise SystemExit(0)  # the hub went away; nothing can answer
            op = message.get("op")
            if op == "ping":
                _channel.send({"op": "pong", "id": message.get("id")})
                continue
            if op == "shutdown":
                raise SystemExit(0)
            if op == "tool_result" and message.get("id") == nested_id:
                if message.get("ok"):
                    return message.get("result")
                error = message.get("error") or {}
                raise ToolCallError(str(error.get("type", "tool_error")), str(error.get("message", "")))
            raise ToolCallError("protocol_error", f"expected tool_result {nested_id}, got {op!r}")


def _signature(fn: object) -> dict:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"params": [], "var_keyword": True, "async": False}
    params = []
    var_keyword = False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            var_keyword = True
            continue
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.POSITIONAL_ONLY):
            continue
        entry: dict[str, object] = {"name": p.name, "required": p.default is inspect.Parameter.empty}
        if p.default is not inspect.Parameter.empty:
            entry["default"] = repr(p.default)
        params.append(entry)
    return {"params": params, "var_keyword": var_keyword, "async": inspect.iscoroutinefunction(fn)}


def _error(error_type: str, message: str) -> dict:
    return {"type": error_type, "message": message[:MAX_ERROR_CHARS]}


def _run(functions: dict, message: dict) -> dict:
    global _current_call
    call_id = message.get("id")
    name = message.get("function")
    args = message.get("args") or {}
    fn = functions.get(name)
    if fn is None:
        return {
            "op": "result",
            "id": call_id,
            "ok": False,
            "error": _error("ProtocolError", f"no function {name!r}"),
        }
    if not isinstance(args, dict):
        return {
            "op": "result",
            "id": call_id,
            "ok": False,
            "error": _error("ProtocolError", "args must be an object"),
        }
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as exc:
        return {"op": "result", "id": call_id, "ok": False, "error": _error("ArgumentsMismatch", str(exc))}
    except ValueError:
        pass  # no signature to check (a builtin); the call itself reports a mismatch
    _secrets.clear()
    _secrets.update({str(k): str(v) for k, v in (message.get("secrets") or {}).items()})
    _current_call = call_id
    try:
        result = asyncio.run(fn(**args)) if inspect.iscoroutinefunction(fn) else fn(**args)
    except Exception as exc:  # noqa: BLE001 - a tool's failure is reported to the hub, never raised
        traceback.print_exc(file=sys.stderr)
        return {"op": "result", "id": call_id, "ok": False, "error": _error(type(exc).__name__, str(exc))}
    finally:
        _current_call = None
    try:
        json.dumps(result, default=str, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return {"op": "result", "id": call_id, "ok": False, "error": _error("UnserialisableResult", str(exc))}
    return {"op": "result", "id": call_id, "ok": True, "result": json.loads(json.dumps(result, default=str))}


def _fix_ca_file() -> None:
    """Points `SSL_CERT_FILE` at the system bundle when this interpreter's CA file is gone.

    A relocated virtualenv can name a CA file that no longer exists, which breaks every HTTPS
    tool; the fix lives here, in the process that needs it, not in the hub.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    if not os.path.exists(ssl.get_default_verify_paths().openssl_cafile) and os.path.exists(SYSTEM_CA_FILE):
        os.environ["SSL_CERT_FILE"] = SYSTEM_CA_FILE


def main(argv: list[str] | None = None) -> int:
    """Runs the worker until `shutdown` or end of input; returns the exit status."""
    global _channel
    parser = argparse.ArgumentParser(prog="hal_worker")
    parser.add_argument("--module", required=True)
    parser.add_argument("--functions", default="")
    options = parser.parse_args(argv)
    _channel = _Channel()
    _fix_ca_file()
    sys.modules["hal_worker"] = sys.modules[__name__]
    module = importlib.import_module(options.module)  # an import error exits non-zero before `ready`
    wanted = [name for name in options.functions.split(",") if name]
    functions = {name: getattr(module, name) for name in wanted if callable(getattr(module, name, None))}
    _channel.send(
        {
            "op": "ready",
            "pid": os.getpid(),
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "module": options.module,
            "functions": {name: _signature(fn) for name, fn in functions.items()},
            "missing": [name for name in wanted if name not in functions],
        }
    )
    while True:
        try:
            message = _channel.receive()
        except ValueError as exc:
            _channel.send(
                {"op": "result", "id": None, "ok": False, "error": _error("ProtocolError", str(exc))}
            )
            continue
        if message is None:
            return 0
        op = message.get("op")
        if op == "call":
            reply = _run(functions, message)
            _channel.send(reply)
            _secrets.clear()
        elif op == "ping":
            _channel.send({"op": "pong", "id": message.get("id")})
        elif op == "shutdown":
            return 0
        else:
            error = _error("ProtocolError", f"unknown op {op!r}")
            _channel.send({"op": "result", "id": message.get("id"), "ok": False, "error": error})


if __name__ == "__main__":
    sys.exit(main())

"""Tool functions of the synthetic `probe` pack: each one misbehaves in one known way.

They import `hal_worker`, which exists only inside a hub worker process.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime

from hal_worker import ToolError, call_tool, secret

CHILD_SLEEP_S = "30"  # far longer than any test timeout, so only a kill ends it


def shout(text: str) -> str:
    """Writes to stdout both ways, then returns `text` in capitals."""
    print("printed by the tool")  # noqa: T201 - the test checks this never reaches the protocol
    os.write(1, b"written to fd 1\n")
    return text.upper()


def boom() -> None:
    """Raises a plain exception."""
    raise ValueError("no event matches 'dentist'")


def fail() -> None:
    """Raises the worker's `ToolError`."""
    raise ToolError("quota reached")


def when() -> datetime:
    """Returns a value JSON encodes only through `default=str`."""
    return datetime(2026, 10, 29, 10, 0, tzinfo=UTC)


def nan() -> float:
    """Returns NaN, which strict JSON cannot carry."""
    return float("nan")


async def later(text: str) -> str:
    """An async tool."""
    await asyncio.sleep(0)
    return f"later {text}"


def reveal(name: str) -> str:
    """Returns a secret wrapped in text."""
    return f"the key is {secret(name)}"


def nested(tool: str) -> object:
    """Calls another tool through the hub."""
    return call_tool(tool, {"text": "hi"})


def sleep(seconds: float, pidfile: str = "") -> str:
    """Starts a long child `sleep`, writes its pid to `pidfile`, then sleeps `seconds`."""
    child = subprocess.Popen(["sleep", CHILD_SLEEP_S])
    if pidfile:
        with open(pidfile, "w", encoding="ascii") as handle:
            handle.write(str(child.pid))
    time.sleep(seconds)
    return "slept"


def crash(code: int) -> None:
    """Exits the worker without replying."""
    sys.stderr.write("probe: crashing on purpose\n")
    sys.stderr.flush()
    os._exit(code)


def env() -> dict:
    """The worker's environment and argv."""
    return {"environ": dict(os.environ), "argv": sys.argv}


def cwd() -> str:
    """The worker's working directory."""
    return os.getcwd()


def big(size: int) -> str:
    """A string of `size` characters."""
    return "x" * size


def connect(port: int) -> str:
    """Connects to 127.0.0.1:`port`."""
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        return "connected"

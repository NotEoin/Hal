"""The runner's network drop under bubblewrap (REQ-HUB-70, 01 REQ-SEC-58). Marked `bwrap`.

A `network = false` tool must not reach a listener on the host's loopback; a `network = true`
tool of the same module must. A timeout must still end every process inside the sandbox.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest

from halhub.manifest import ModuleSpec, load_manifest
from halhub.runner import Runner, RunnerConfig, bwrap_available

pytestmark = pytest.mark.bwrap

CONFIG = RunnerConfig(kill_grace=0.3, bwrap="auto")


@pytest.fixture
def probe(fixtures) -> ModuleSpec:
    return load_manifest(fixtures / "probe" / "hal-module.toml", source="path")


@pytest.fixture
def runner():
    if not bwrap_available():
        pytest.skip("bubblewrap cannot start an unprivileged sandbox here")
    r = Runner(CONFIG)
    yield r
    r.close()


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except TimeoutError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    server.close()


def tool(module: ModuleSpec, name: str):
    return next(t for t in module.tools if t.name == name)


def test_tool_without_network_cannot_open_a_socket(runner, probe, listener):
    outcome = runner.run(probe, tool(probe, "probe_connect"), {"port": listener}, timeout=5)
    assert not outcome.ok and outcome.error_type == "tool_error"


def test_tool_with_network_reaches_a_local_listener(runner, probe, listener):
    outcome = runner.run(probe, tool(probe, "probe_online"), {"port": listener}, timeout=5)
    assert outcome.ok and outcome.result == "connected"


def test_sandbox_status_reports_the_drop(runner):
    assert runner.sandbox_status().startswith("network drop on")


def host_pids_with_inner_pid(inner: int) -> list[int]:
    """Host pids of `sleep` processes whose innermost namespace pid is `inner`."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            status = (entry / "status").read_text()
        except OSError:
            continue
        nspid = next((line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")), [])
        if cmdline[0] == b"sleep" and len(nspid) > 1 and int(nspid[-1]) == inner:
            found.append(int(entry.name))
    return found


def test_timeout_kills_every_process_in_the_sandbox(runner, probe, tmp_path):
    pidfile = tmp_path / "child.pid"
    outcome = runner.run(
        probe, tool(probe, "probe_sleep"), {"seconds": 30, "pidfile": str(pidfile)}, timeout=0.5
    )
    assert outcome.error_type == "timeout"
    assert host_pids_with_inner_pid(int(pidfile.read_text())) == []
    with pytest.raises(ProcessLookupError):
        os.kill(outcome.worker_pid, 0)

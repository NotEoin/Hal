"""What the restricted keys allow and refuse, against a real unprivileged sshd (REQ-RMT-04, 05)."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from halremote import authkeys, keys
from halremote.model import Forward

from .conftest import free_port, wait_listening

pytestmark = pytest.mark.slow
TIMEOUT_S = 10


def _authorize(
    server, name, role, *, port, serve=Path("/bin/true"), hub=Path("/bin/true"), from_pattern="127.0.0.0/8"
):
    key = server.new_key(name)
    pub = keys.parse_public_key(key.with_suffix(".pub").read_text())
    server.authorize(
        authkeys.render_line(
            role,
            client="laptop",
            key=pub,
            from_pattern=from_pattern,
            forwards=[Forward("gateway", port, port)],
            serve_path=serve,
            hub_path=hub,
        )
    )
    return key


def _read_through(local_port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", local_port), timeout=TIMEOUT_S) as sock:
        sock.settimeout(TIMEOUT_S)
        try:
            return sock.recv(16)
        except OSError:
            return b""


def test_tunnel_key_reaches_the_permitted_port(start_sshd, echo_server):
    server = start_sshd()
    key = _authorize(server, "tunnel", "tunnel", port=echo_server)
    local = free_port()
    proc = server.hold(key, "-o", "ExitOnForwardFailure=yes", "-L", f"{local}:127.0.0.1:{echo_server}")
    assert wait_listening(local, TIMEOUT_S, proc)
    assert _read_through(local) == b"hello"


def test_tunnel_key_is_refused_a_port_it_was_not_given(start_sshd, echo_server):
    server = start_sshd()
    key = _authorize(server, "tunnel", "tunnel", port=echo_server)
    other = free_port()
    local = free_port()
    proc = server.hold(key, "-L", f"{local}:127.0.0.1:{other}")
    assert wait_listening(local, TIMEOUT_S, proc)
    assert _read_through(local) == b""


def test_tunnel_key_cannot_open_a_tcp_remote_listener(start_sshd, echo_server):
    server = start_sshd()
    key = _authorize(server, "tunnel", "tunnel", port=echo_server)
    res = server.run(
        key, "-N", "-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{free_port()}:127.0.0.1:{echo_server}"
    )
    assert res.returncode != 0


def test_tunnel_key_gets_no_command(start_sshd, echo_server):
    server = start_sshd()
    key = _authorize(server, "tunnel", "tunnel", port=echo_server)
    res = server.run(key, command="echo shell-ran")
    assert res.returncode == 1
    assert "shell-ran" not in res.stdout


def test_remote_socket_listener_follows_the_server_setting(start_sshd, echo_server, tmp_path):
    """Documents the residual risk of Decision D7: only the server line stops `-R` to a socket."""
    open_server = start_sshd()
    key = _authorize(open_server, "tunnel", "tunnel", port=echo_server)
    sock = Path("/tmp") / f"hal-test-{free_port()}.sock"
    proc = open_server.hold(key, "-o", "ExitOnForwardFailure=yes", "-R", f"{sock}:127.0.0.1:{echo_server}")
    try:
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline and not sock.exists() and proc.poll() is None:
            time.sleep(0.1)
        assert sock.exists()
    finally:
        proc.kill()
        sock.unlink(missing_ok=True)
    closed = start_sshd("AllowStreamLocalForwarding no")
    key2 = _authorize(closed, "tunnel", "tunnel", port=echo_server)
    res = closed.run(
        key2, "-N", "-o", "ExitOnForwardFailure=yes", "-R", f"{tmp_path / 'x.sock'}:127.0.0.1:{echo_server}"
    )
    assert res.returncode != 0


def test_mcp_key_ignores_the_client_command(start_sshd, hal_remote_exe, fake_hub):
    server = start_sshd()
    key = _authorize(server, "mcp", "mcp", port=1, serve=hal_remote_exe, hub=fake_hub)
    res = server.run(key, command="rm -rf x")
    assert res.returncode == 64
    assert '"error": "unknown_verb"' in res.stdout


def test_mcp_key_cannot_forward_ports(start_sshd, hal_remote_exe, fake_hub, echo_server):
    server = start_sshd()
    key = _authorize(server, "mcp", "mcp", port=1, serve=hal_remote_exe, hub=fake_hub)
    local = free_port()
    proc = server.hold(key, "-L", f"{local}:127.0.0.1:{echo_server}")
    assert wait_listening(local, TIMEOUT_S, proc)
    assert _read_through(local) == b""


def test_mcp_key_is_refused_a_pty(start_sshd, hal_remote_exe, fake_hub):
    server = start_sshd()
    key = _authorize(server, "mcp", "mcp", port=1, serve=hal_remote_exe, hub=fake_hub)
    res = server.run(key, "-tt", command="check")
    assert "PTY allocation request failed" in res.stderr


def test_key_from_another_range_is_refused(start_sshd, echo_server):
    server = start_sshd()
    key = _authorize(server, "tunnel", "tunnel", port=echo_server, from_pattern="203.0.113.0/24")
    res = server.run(key, command="true")
    assert res.returncode == 255

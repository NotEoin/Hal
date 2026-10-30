"""Tests for `halsandbox.transport`: framing, caps, timeouts, peer checks (REQ-BOX-29 to 31, 65)."""

import json
import os
import socket
import time

import pytest

from halsandbox.transport import LineServer, MessageTooLarge, PeerRule

UID = os.getuid()


def _echo(conn):
    while (msg := conn.read_message()) is not None:
        conn.write_message(b"re:" + msg)


@pytest.fixture
def make(tmp_path):
    servers = []

    def build(handler=_echo, peer=None, **kw):
        s = LineServer(tmp_path / "t.sock", handler, peer=peer or PeerRule(UID), **kw)
        s.start()
        servers.append(s)
        return s

    yield build
    for s in servers:
        s.close()


def _dial(server):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(5)
    c.connect(str(server.path))
    return c


def _read_all(c):
    out = b""
    try:
        while chunk := c.recv(65536):
            out += chunk
    except OSError:
        pass
    return out


def test_messages_pass_unchanged(make):
    c = _dial(make())
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    c.sendall(raw + b"\n")
    assert c.makefile("rb").readline() == b"re:" + raw + b"\n"


def test_message_split_across_packets_is_delivered_whole(make):
    c = _dial(make())
    c.sendall(b"ab")
    time.sleep(0.05)
    c.sendall(b"cd\n")
    assert c.makefile("rb").readline() == b"re:abcd\n"


def test_exactly_the_limit_passes_and_one_byte_over_fails(make):
    def count(conn):
        while (msg := conn.read_message()) is not None:
            conn.write_message(str(len(msg)).encode())

    c = _dial(make(count, max_message_bytes=100))
    f = c.makefile("rb")
    c.sendall(b"x" * 100 + b"\n")
    assert f.readline() == b"100\n"
    c.sendall(b"y" * 101 + b"\n")
    assert json.loads(f.readline())["error"]["code"] == -32600


def test_oversize_message_gets_error_then_close(make):
    c = _dial(make(max_message_bytes=100))
    c.sendall(b"z" * 150)  # no newline at all
    frame = json.loads(_read_all(c).splitlines()[0])
    assert frame["id"] is None and frame["error"]["code"] == -32600 and "100" in frame["error"]["message"]


def test_oversize_reply_is_refused_by_write_message(make):
    seen = []

    def handler(conn):
        try:
            conn.write_message(b"x" * 101)
        except MessageTooLarge as exc:
            seen.append(exc)

    c = _dial(make(handler, max_message_bytes=100))
    _read_all(c)
    assert seen


def test_slow_partial_message_times_out(make):
    c = _dial(make(partial_timeout_s=0.2))
    c.sendall(b"half")
    t0 = time.monotonic()
    assert b"-32600" in _read_all(c)
    assert time.monotonic() - t0 < 3


def test_idle_connection_with_nothing_pending_is_kept(make):
    c = _dial(make(partial_timeout_s=0.2))
    time.sleep(0.5)
    c.sendall(b"hi\n")
    assert c.makefile("rb").readline() == b"re:hi\n"


def test_connection_cap(make):
    s = make(max_connections=1)
    first = _dial(s)
    first.sendall(b"a\n")
    first.makefile("rb").readline()
    second = _dial(s)
    assert json.loads(_read_all(second))["error"]["message"] == "too many connections"
    first.sendall(b"b\n")
    assert first.makefile("rb").readline() == b"re:b\n"


def test_wrong_peer_uid_is_refused(make):
    s = make(peer_cred=lambda _sock: (1, UID + 1, 1))
    c = _dial(s)
    assert _read_all(c) == b""
    assert s.connections() == 0


def test_peer_outside_the_container_cgroup_is_refused(make):
    s = make(peer=PeerRule(UID, container_id="deadbeef" * 8))
    assert _read_all(_dial(s)) == b""


def test_peer_inside_the_container_cgroup_is_served(make, monkeypatch):
    monkeypatch.setattr("halsandbox.transport._in_container", lambda pid, cid: cid == "abc123")
    c = _dial(make(peer=PeerRule(UID, container_id="abc123")))
    c.sendall(b"ok\n")
    assert c.makefile("rb").readline() == b"re:ok\n"


def test_socket_is_private_and_removed_on_close(make, tmp_path):
    s = make()
    assert (tmp_path / "t.sock").stat().st_mode & 0o777 == 0o600
    s.close()
    assert not (tmp_path / "t.sock").exists()


def test_message_with_a_newline_cannot_be_written(make):
    errors = []

    def handler(conn):
        try:
            conn.write_message(b"a\nb")
        except ValueError as exc:
            errors.append(exc)

    _read_all(_dial(make(handler)))
    assert errors

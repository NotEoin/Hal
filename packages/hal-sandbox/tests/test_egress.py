"""Tests for the dependency-prep CONNECT proxy, with a fake resolver and a fake upstream."""

from __future__ import annotations

import socket
import threading

import pytest

from halkit.testing import FakeResolver
from halsandbox.egress import DEFAULT_REGISTRIES, ConnectProxy

PUBLIC = "93.184.216.34"  # a global documentation-style address; nothing connects to it


class Harness:
    def __init__(self, sockdir, resolver, **kw):
        self.events: list[dict] = []
        self.connected: list[tuple[str, int]] = []
        self.upstream_peers: list[socket.socket] = []
        self.path = sockdir / "egress.sock"
        self.proxy = ConnectProxy(
            self.path,
            registries=DEFAULT_REGISTRIES,
            on_event=self.events.append,
            resolve=resolver,
            connect=self._connect,
            **kw,
        )
        self.proxy.start()

    def _connect(self, address: str, port: int) -> socket.socket:
        self.connected.append((address, port))
        near, far = socket.socketpair()
        self.upstream_peers.append(far)

        def echo() -> None:
            while data := far.recv(4096):
                far.sendall(data.upper())

        threading.Thread(target=echo, daemon=True).start()
        return near

    def client(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        s.connect(str(self.path))
        return s

    def ask(self, request: bytes) -> bytes:
        with self.client() as s:
            s.sendall(request)
            return s.recv(4096)


@pytest.fixture
def make(sockdir):
    made = []

    def build(table=None, **kw):
        h = Harness(sockdir, FakeResolver(table or {"pypi.org": [PUBLIC]}), **kw)
        made.append(h)
        return h

    yield build
    for h in made:
        h.proxy.close()
        for p in h.upstream_peers:
            p.close()


def test_only_listed_hosts_on_443(make):
    h = make()
    assert h.ask(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 200")
    assert h.connected == [(PUBLIC, 443)]
    for bad in (
        b"CONNECT pypi.org:80 HTTP/1.1\r\n\r\n",
        b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n",
        b"CONNECT sub.pypi.org:443 HTTP/1.1\r\n\r\n",
        b"GET http://pypi.org/ HTTP/1.1\r\n\r\n",
    ):
        assert h.ask(bad).startswith(b"HTTP/1.1 403")
    assert len(h.proxy.denied) == 4 and len(h.connected) == 1


def test_a_denied_host_is_never_resolved(make):
    h = make()
    h.ask(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
    assert h.proxy._resolve.lookups == []


def test_private_address_is_refused(make):
    h = make({"pypi.org": ["10.0.0.5"]})
    assert h.ask(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert h.connected == []
    assert any("address refused" in str(e["reason"]) for e in h.events if e["event"] == "egress.deny")


def test_one_private_address_among_public_ones_refuses_the_host(make):
    h = make({"pypi.org": [PUBLIC, "127.0.0.1"]})
    assert h.ask(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_the_connect_uses_the_checked_address_and_bytes_flow_both_ways(make):
    h = make()
    with h.client() as s:
        s.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"hello")
        assert s.recv(4096) == b"HELLO"
    assert h.connected == [(PUBLIC, 443)]
    assert [e["event"] for e in h.events] == ["egress.allow"]


def test_a_client_that_sends_no_head_times_out(make):
    h = make(head_timeout_s=0.2)
    with h.client() as s:
        assert s.recv(4096).startswith(b"HTTP/1.1 408")


def test_an_idle_tunnel_is_closed(make):
    h = make(idle_timeout_s=0.2)
    with h.client() as s:
        s.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        assert s.recv(4096) == b""


def test_tunnels_past_the_cap_get_503(make):
    h = make(max_tunnels=1)
    with h.client() as first:
        first.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        assert first.recv(4096).startswith(b"HTTP/1.1 200")
        assert h.ask(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 503")


def test_the_socket_is_private(make):
    assert make().path.stat().st_mode & 0o777 == 0o600

"""Integration tests for `halgateway.server` and `halgateway.handler` on real sockets."""

import http.client
import json
import socket
import stat
import time

import pytest

from halgateway.config import load_config
from halgateway.server import Gateway, StartError
from halkit import paths

TOKEN = "hal-gw-integration-test"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(self._path))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def make(tmp_path):
    started = []

    def build(extra="", **kwargs):
        token = tmp_path / "token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            f'[gateway]\nunix_path = "{tmp_path}/run/gateway.sock"\ntoken_file = "{token}"\n{extra}\n'
        )
        gw = Gateway(load_config(cfg_file), **kwargs)
        started.append(gw)
        return gw

    yield build
    for gw in started:
        gw.stop()


def get(gw, path="/health", headers=None):
    conn = UnixConnection(gw.socket_path())
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, resp.read(), resp


def tcp_get(port, path="/health", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_unix_socket_is_owner_only(make):
    gw = make()
    gw.start()
    assert stat.S_IMODE(gw.socket_path().stat().st_mode) == 0o600


def test_start_refuses_group_writable_socket_dir(make, tmp_path):
    run = tmp_path / "run"
    run.mkdir(mode=0o770)
    run.chmod(0o770)
    with pytest.raises(StartError, match="not private"):
        make().start()


def test_missing_runtime_dir_fails_loudly(tmp_path, monkeypatch):
    def boom():
        raise paths.RuntimeDirMissing("no runtime dir")

    monkeypatch.setattr(paths, "runtime_dir", boom)
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    with pytest.raises(StartError, match=r"HAL_RUNTIME_DIR.*unix_path"):
        Gateway(load_config(cfg)).socket_path()


def test_overlong_socket_path_is_rejected(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\nunix_path = "{tmp_path}/{"x" * 120}/gateway.sock"\n')
    with pytest.raises(StartError, match="shorter"):
        Gateway(load_config(cfg)).socket_path()


def test_health_is_public_and_local(make):
    gw = make()
    gw.start()
    status, body, _ = get(gw)
    assert (status, json.loads(body)) == (200, {"status": "ok"})


def test_unknown_route_for_the_owner_is_404(make):
    gw = make()
    gw.start()
    assert get(gw, "/v1/nothing")[0] == 404


def test_foreign_uid_peer_is_rejected(make):
    gw = make(peer_uid=lambda sock: 4242, own_uid=1000)
    gw.start()
    assert get(gw)[0] == 403


def test_stale_socket_is_replaced(make):
    first = make()
    first.start()
    first.unix.shutdown()  # the file stays, nobody answers
    first.unix.server_close()
    second = make()
    second.start()
    assert get(second)[0] == 200


def test_second_gateway_refuses_to_start(make):
    make().start()
    with pytest.raises(StartError, match="another gateway"):
        make().start()


def test_tcp_is_off_by_default(make):
    gw = make()
    gw.start()
    assert gw.tcp is None


def test_tcp_request_without_token_is_401(make):
    port = free_port()
    make(f"tcp = true\ntcp_port = {port}").start()
    assert tcp_get(port, "/v1/models")[0] == 401


def test_x_api_key_header_is_accepted(make):
    port = free_port()
    make(f"tcp = true\ntcp_port = {port}").start()
    assert tcp_get(port, "/v1/models", {"X-Api-Key": TOKEN})[0] == 200


def test_tcp_health_needs_no_token(make):
    port = free_port()
    make(f"tcp = true\ntcp_port = {port}").start()
    assert tcp_get(port)[0] == 200


def test_dns_rebinding_host_is_403(make):
    port = free_port()
    make(f"tcp = true\ntcp_port = {port}").start()
    assert tcp_get(port, headers={"Host": "evil.example"})[0] == 403


def test_browser_origin_is_403(make):
    port = free_port()
    make(f"tcp = true\ntcp_port = {port}").start()
    assert tcp_get(port, headers={"Origin": "http://page.example"})[0] == 403


def test_world_readable_token_file_blocks_tcp(make, tmp_path):
    gw = make(f"tcp = true\ntcp_port = {free_port()}")
    (tmp_path / "token").chmod(0o644)
    with pytest.raises(StartError, match="readable by others"):
        gw.start()


def test_token_file_is_created_at_first_start(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[gateway]\nunix_path = "{tmp_path}/run/g.sock"\ntoken_file = "{tmp_path}/new-token"\n')
    gw = Gateway(load_config(cfg))
    gw.start()
    try:
        assert stat.S_IMODE((tmp_path / "new-token").stat().st_mode) == 0o600
    finally:
        gw.stop()


# a loaded machine took over 3 s to answer once while a full CV eval ran beside the suite (2026-11-12)
CAP_WAIT_S = 10


def test_connection_cap_is_503(make):
    gw = make("max_connections = 1")
    gw.start()
    hold = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    hold.connect(str(gw.socket_path()))  # takes the only slot and sends nothing
    try:
        deadline = time.monotonic() + CAP_WAIT_S
        status = 0
        while time.monotonic() < deadline and status != 503:
            conn = UnixConnection(gw.socket_path())
            conn.request("GET", "/health")
            resp = conn.getresponse()
            status, retry = resp.status, resp.getheader("Retry-After")
            time.sleep(0.05)
        assert (status, retry) == (503, "1")
    finally:
        hold.close()

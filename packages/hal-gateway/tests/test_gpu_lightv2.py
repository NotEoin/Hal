"""Tests for the gpu-light v2 client against a fake daemon (REQ-GW-50, 51, 53, 59)."""

import json
import socket
import threading
import time

import pytest

from halgateway import gpu
from halgateway.gpu import lightv2
from halgateway.gpu.manager import GpuManager, make_coordinator

SPEC = lightv2.reclaimer_spec("hal-gateway", "/opt/hal/bin/hal-gateway")


class FakeDaemon:
    """A minimal gpu-light daemon: records requests and answers them from a script."""

    def __init__(self, path, protocol=2):
        self.path, self.protocol = path, protocol
        self.requests, self.reclaimers, self.grant_now = [], [], True
        self.claim_error, self.never_grant = None, False
        self.conns = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _send(self, conn, doc):
        try:
            conn.sendall(json.dumps(doc).encode() + b"\n")
        except OSError:
            pass  # the test closed the connection

    def _serve(self, conn):
        for line in conn.makefile("rb"):
            req = json.loads(line)
            self.requests.append((req["method"], req.get("params", {})))
            result, error = self.answer(req)
            doc = {"jsonrpc": "2.0", "id": req["id"]}
            doc.update({"error": error} if error else {"result": result})
            self._send(conn, doc)
            if req["method"] == "claim" and not error and not self.grant_now and not self.never_grant:
                note = {
                    "jsonrpc": "2.0",
                    "method": "claim_granted",
                    "params": {"name": req["params"]["name"]},
                }
                threading.Timer(0.05, self._send, (conn, note)).start()

    def answer(self, req):
        method, params = req["method"], req.get("params", {})
        if method == "hello":
            return {"server": "gpu-light", "protocol": self.protocol}, None
        if method == "reclaimers.list":
            return {"reclaimers": self.reclaimers}, None
        if method == "reclaimers.add":
            self.reclaimers.append({"name": params["spec"]["name"], "source": "cli", "spec": params["spec"]})
            return {"reclaimer": params["spec"]}, None
        if method == "reclaimers.remove":
            self.reclaimers = [r for r in self.reclaimers if r["name"] != params["name"]]
            return {"removed": True}, None
        if method == "claim":
            if self.claim_error and "uses" in params:
                return None, {"code": self.claim_error, "message": "no such reclaimer"}
            return {"claim": {"name": params["name"], "granted": self.grant_now}}, None
        if method == "claim.release":
            return {"released": True}, None
        return None, {"code": -32601, "message": "method"}

    def methods(self):
        return [m for m, _ in self.requests]

    def drop(self):
        for conn in self.conns:
            conn.shutdown(socket.SHUT_RDWR)
        self.conns.clear()

    def close(self):
        self.srv.close()
        self.drop()


@pytest.fixture
def daemon(tmp_path):
    d = FakeDaemon(tmp_path / "gl.sock")
    yield d
    d.close()


def coordinator(daemon, **kw):
    return lightv2.V2Coordinator(daemon.path, reclaimer=SPEC, **kw)


def claim(coord, wait_s=2.0, uses=("hal-gateway",)):
    return coord.acquire(
        "hal-gateway-interactive",
        cls="interactive",
        wait_s=wait_s,
        vram_mib=22000,
        uses=list(uses),
        reason="r",
    )


def test_v2_claim_follows_gl_methods(daemon):
    coord = coordinator(daemon)
    result = claim(coord)
    assert result.held
    assert daemon.methods() == ["hello", "reclaimers.list", "reclaimers.add", "claim"]
    params = dict(daemon.requests)["claim"]
    assert params == {
        "name": "hal-gateway-interactive", "class": "interactive", "gpu": 0, "exclusive": True,
        "uses": ["hal-gateway"], "reason": "r", "lifetime": "connection", "vram_mib": 22000,
    }  # fmt: skip
    assert dict(daemon.requests)["hello"]["protocol"] == 2
    coord.release("hal-gateway-interactive")
    assert daemon.methods()[-1] == "claim.release"
    coord.close()


def test_claim_waits_for_the_granted_notification(daemon):
    daemon.grant_now = False
    coord = coordinator(daemon)
    result = claim(coord)
    assert result.held and result.waited_s >= 0.04
    coord.close()


def test_claim_wait_timeout_releases_the_claim(daemon):
    daemon.grant_now, daemon.never_grant = False, True
    coord = coordinator(daemon)
    result = claim(coord, wait_s=0.1)
    assert not result.held and "did not grant" in result.warning
    assert daemon.methods()[-1] == "claim.release" and coord.status()["claims"] == []
    coord.close()


def test_unknown_reclaimer_in_uses_retries_without_uses(daemon):
    daemon.claim_error = lightv2.NOT_FOUND
    coord = coordinator(daemon)
    assert claim(coord).held
    claims = [p for m, p in daemon.requests if m == "claim"]
    assert "uses" in claims[0] and "uses" not in claims[1]
    coord.close()


def test_reclaimer_is_registered_once(daemon):
    coord = coordinator(daemon)
    claim(coord)
    coord.close()
    coord2 = coordinator(daemon)
    claim(coord2)
    assert daemon.methods().count("reclaimers.add") == 1
    coord2.close()


def test_stale_cli_reclaimer_is_replaced(daemon):
    daemon.reclaimers = [{"name": "hal-gateway", "source": "cli", "spec": {**SPEC, "timeout_s": 5}}]
    coord = coordinator(daemon)
    claim(coord)
    assert "reclaimers.remove" in daemon.methods() and "reclaimers.add" in daemon.methods()
    coord.close()


def test_config_reclaimer_is_not_touched(daemon, caplog):
    daemon.reclaimers = [{"name": "hal-gateway", "source": "config", "spec": {**SPEC, "timeout_s": 5}}]
    coord = coordinator(daemon)
    claim(coord)
    assert "reclaimers.remove" not in daemon.methods() and "reclaimers.add" not in daemon.methods()
    assert "left alone" in caplog.text
    coord.close()


def test_reclaimer_spec_uses_the_absolute_console_script_path():
    assert SPEC["reclaim"] == ["/opt/hal/bin/hal-gateway", "release-gpu", "--reclaim"]
    assert SPEC["type"] == "command" and SPEC["timeout_s"] == 60 and SPEC["idle_after_s"] == 120


def test_claims_are_sent_again_after_a_reconnect(daemon):
    clock = [0.0]
    coord = coordinator(daemon, clock=lambda: clock[0])
    claim(coord)
    daemon.drop()
    time.sleep(0.2)
    assert not coord.connected()
    coord.maintain()
    assert coord.connected()
    assert [m for m in daemon.methods() if m == "claim"] == ["claim", "claim"]
    coord.close()


def test_unreachable_daemon_backs_off_and_does_not_claim(tmp_path):
    clock = [100.0]
    coord = lightv2.V2Coordinator(tmp_path / "none.sock", reclaimer=SPEC, clock=lambda: clock[0])
    assert not claim(coord).held
    assert coord._next_try == 101.0
    assert not coord.connect()  # still backing off
    clock[0] = 101.5
    assert not coord.connect() and coord._next_try == 103.5  # doubled


def test_v2_protocol_mismatch_is_not_used(tmp_path):
    d = FakeDaemon(tmp_path / "gl3.sock", protocol=3)
    try:
        assert not lightv2.probe(d.path)
    finally:
        d.close()


def test_coordinator_autodetect(tmp_path, monkeypatch):
    v1 = tmp_path / "v1"
    v1.mkdir(mode=0o700)
    sock = tmp_path / "none.sock"
    assert gpu.detect(str(tmp_path / "missing"), str(sock)) == "none"
    assert gpu.detect(str(v1), str(sock)) == "gpu-light-v1"
    d = FakeDaemon(tmp_path / "gl.sock")
    try:
        assert gpu.detect(str(v1), str(d.path)) == "gpu-light-v2"
        cfg = {"coordinator": "auto", "v2_socket": str(d.path), "v1_dir": str(v1), "v1_claim_name": "x",
               "reclaimer_name": "hal-gateway"}  # fmt: skip
        assert make_coordinator(cfg).kind == "gpu-light-v2"
        assert make_coordinator({**cfg, "coordinator": "gpu-light-v1"}).kind == "gpu-light-v1"
        assert make_coordinator({**cfg, "coordinator": "none"}) is None
    finally:
        d.close()


def test_v2_idle_releases_claim_and_keeps_models(tmp_path, daemon):
    from types import SimpleNamespace

    from halgateway.auth import Principal

    freed = []
    backend = SimpleNamespace(
        name="l", gpu=True, type="llama-router", entry={"gpu_light_uses": ["hal-gateway"]},
        free_vram=lambda served: freed.append(1) or [],
    )  # fmt: skip
    cfg = {
        "respect_off_flag": False, "owner_unix_class": "service", "owner_tcp_class": "interactive",
        "v1_claim_wait_s": 1.0, "claim_wait_s": 2.0, "on_busy": "proceed", "reclaimer_name": "hal-gateway",
        "v1_claim_name": "g", "vram_mib": {}, "idle_unload_s": 900.0, "idle_release_s": 120.0,
    }  # fmt: skip
    now = [1000.0]
    mgr = GpuManager(
        cfg, {"l": backend}, coordinator(daemon), state_dir=tmp_path / "s" / "g", clock=lambda: now[0]
    )
    target = SimpleNamespace(backend=backend, model="m", alias="a")
    with mgr.request(target, Principal("owner", "unix")):
        pass
    assert dict(daemon.requests)["claim"]["name"] == "hal-gateway-service"
    mgr.tick(now[0] + 119)
    assert "claim.release" not in daemon.methods()
    mgr.tick(now[0] + 121)
    assert daemon.methods()[-1] == "claim.release" and freed == []
    mgr.tick(now[0] + 5000)
    assert freed == []  # v2 never unloads on idle: gpu-light frees the models through the reclaimer
    mgr.close()

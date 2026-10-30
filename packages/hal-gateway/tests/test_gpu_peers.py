"""Tests for the peer GPU guard (REQ-GW-56)."""

import json
import urllib.error

import pytest

from halgateway.config import load_config
from halgateway.gpu.manager import GpuManager
from halgateway.gpu.peers import (
    Peer,
    PeerBusy,
    PeerConfigError,
    PeerGuard,
    fetch_models,
    holds_gpu,
    parse_peers,
)

LOADED = {"data": [{"id": "a", "status": {"value": "unloaded"}}, {"id": "b", "status": {"value": "loaded"}}]}
IDLE = {"data": [{"id": "a", "status": {"value": "unloaded"}}]}


def guard(doc, peers=("http://127.0.0.1:1",)):
    return PeerGuard([Peer(u) for u in peers], 1.0, fetch=lambda peer, t: doc)


def test_peer_holding_gpu_blocks_load():
    with pytest.raises(PeerBusy, match="127.0.0.1:1.*b") as err:
        guard(LOADED).check()
    assert err.value.status == 409 and err.value.code == "gpu_busy_peer"


def test_idle_peer_does_not_block():
    guard(IDLE).check()


def test_unreachable_peer_does_not_block(caplog):
    def down(peer, timeout):
        raise urllib.error.URLError("refused")

    PeerGuard([Peer("http://127.0.0.1:1")], 1.0, fetch=down).check()
    assert "unreachable" in caplog.text


def test_unparsable_answer_does_not_block():
    def bad(peer, timeout):
        raise json.JSONDecodeError("x", "", 0)

    PeerGuard([Peer("http://127.0.0.1:1")], 1.0, fetch=bad).check()


def test_loaded_is_read_from_json_not_from_the_text():
    assert holds_gpu({"data": [{"id": "x", "note": "loaded"}]}) == []
    assert holds_gpu("loaded") == []


def test_peers_are_validated():
    assert parse_peers([{"url": "http://h:1/"}]) == [Peer("http://h:1")]
    for bad in ([{}], [{"url": "ftp://h"}], [{"url": "http://h", "x": 1}], ["http://h"]):
        with pytest.raises(PeerConfigError):
            parse_peers(bad)


def test_config_rejects_a_bad_peer(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[gateway.gpu.peers]]\nurl = "nope"\n')
    with pytest.raises(Exception, match="peers"):
        load_config(cfg)


def test_fetch_reads_the_key_file_and_models_route(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("secret\n")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b'{"data": []}'

    def fake_open(req, timeout):
        seen["url"], seen["auth"], seen["timeout"] = req.full_url, req.get_header("Authorization"), timeout
        return Resp()

    monkeypatch.setattr("halgateway.gpu.peers.urllib.request.urlopen", fake_open)
    assert fetch_models(Peer("http://h:1", str(key)), 2.0) == {"data": []}
    assert seen == {"url": "http://h:1/models", "auth": "Bearer secret", "timeout": 2.0}


def test_manager_checks_peers_before_a_claim_and_takes_none_when_blocked(tmp_path):
    from types import SimpleNamespace

    from halgateway.auth import Principal

    class Coord:
        kind = "gpu-light-v1"
        calls = 0

        def acquire(self, *a, **k):
            Coord.calls += 1

    backend = SimpleNamespace(name="l", gpu=True, type="llama-router", entry={}, model="m")
    cfg = {
        "respect_off_flag": False, "owner_unix_class": "service", "owner_tcp_class": "interactive",
        "v1_claim_wait_s": 1.0, "claim_wait_s": 1.0, "on_busy": "proceed", "reclaimer_name": "g",
        "v1_claim_name": "g", "vram_mib": {}, "idle_unload_s": 0.0,
    }  # fmt: skip
    mgr = GpuManager(cfg, {"l": backend}, Coord(), state_dir=tmp_path / "s" / "g", guard=guard(LOADED))
    target = SimpleNamespace(backend=backend, model="m", alias="a")
    with pytest.raises(PeerBusy), mgr.request(target, Principal("owner", "unix")):
        pass
    assert Coord.calls == 0

"""Tests for the GPU manager: off flag, claims, idle unload and release (REQ-GW-51 to 57)."""

from types import SimpleNamespace

import pytest

from halgateway.auth import Principal
from halgateway.errors import Unavailable
from halgateway.gpu import AcquireResult, LeaseInfo
from halgateway.gpu.manager import GpuBusy, GpuManager

CFG = {
    "respect_off_flag": True, "idle_unload_s": 900.0, "claim_wait_s": 10.0, "v1_claim_wait_s": 120.0,
    "on_busy": "proceed", "owner_unix_class": "service", "owner_tcp_class": "interactive",
    "reclaimer_name": "hal-gateway", "v1_claim_name": "hal-gateway", "vram_mib": {"big": 20000},
}  # fmt: skip


class FakeBackend:
    def __init__(self, name="local", gpu=True, type_="llama-router"):
        self.name, self.gpu, self.type = name, gpu, type_
        self.entry = {"gpu_light_uses": [], "unload_offloaded_on_claim": True}
        self.freed = []
        self.offloaded = []

    def free_vram(self, served):
        self.freed.append(sorted(served))
        return ["m"]

    def unload_offloaded(self):
        return self.offloaded


class FakeCoordinator:
    kind = "gpu-light-v1"

    def __init__(self, held=True):
        self.calls, self.held = [], held

    def acquire(self, name, **kw):
        self.calls.append(("acquire", name, kw))
        return AcquireResult(self.held, 0.0, "" if self.held else "busy")

    def release(self, name):
        self.calls.append(("release", name))

    def status(self):
        return {"kind": self.kind}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path, backend=None, coord="default", **cfg):
    backend = backend or FakeBackend()
    coordinator = FakeCoordinator() if coord == "default" else coord
    clock = Clock()
    mgr = GpuManager(
        {**CFG, **cfg},
        {backend.name: backend},
        coordinator,
        state_dir=tmp_path / "state" / "gateway",
        clock=clock,
    )
    return mgr, backend, coordinator, clock


def target(backend, model="m"):
    return SimpleNamespace(backend=backend, model=model, alias="hal/main")


OWNER = Principal("owner", "unix")


def test_off_flag_blocks_gpu_backends_only(tmp_path):
    mgr, backend, coord, _ = make(tmp_path)
    mgr.off_flag.parent.mkdir(parents=True)
    mgr.off_flag.write_text("")
    with pytest.raises(Unavailable, match="hal on"), mgr.request(target(backend), OWNER):
        pass
    assert coord.calls == []  # no claim is taken while off
    with mgr.request(target(FakeBackend("cpu", gpu=False)), OWNER):
        pass


def test_leased_session_works_while_off(tmp_path):
    mgr, backend, coord, _ = make(tmp_path)
    mgr.off_flag.parent.mkdir(parents=True)
    mgr.off_flag.write_text("")
    with mgr.request(target(backend), Principal("session", "unix", lease=True)):
        pass
    assert coord.calls[0][0] == "acquire"


def test_claim_is_taken_once_per_class_with_the_configured_vram(tmp_path):
    mgr, backend, coord, _ = make(tmp_path)
    for _ in range(2):
        with mgr.request(target(backend, "big"), OWNER):
            pass
    assert [c[0] for c in coord.calls] == ["acquire"]
    assert coord.calls[0][2]["vram_mib"] == 20000 and coord.calls[0][2]["cls"] == "service"


def test_claim_class_follows_the_listener(tmp_path):
    mgr, *_ = make(tmp_path)
    assert mgr.claim_class(Principal("owner", "tcp")) == "interactive"
    assert mgr.claim_class(Principal("owner", "unix")) == "service"
    assert mgr.claim_class(Principal("client", "tcp", cls="service")) == "service"


def test_a_batch_client_claims_as_batch(tmp_path):
    coord = FakeCoordinator()
    coord.kind = "gpu-light-v2"
    mgr, backend, coord, _ = make(tmp_path, coord=coord)
    with mgr.request(target(backend), Principal("client", "unix", cls="batch")):
        pass
    assert coord.calls[0][1] == "hal-gateway-batch" and coord.calls[0][2]["cls"] == "batch"


def test_a_batch_request_never_runs_without_its_claim(tmp_path):
    mgr, backend, _, _ = make(tmp_path, coord=FakeCoordinator(held=False))
    with pytest.raises(GpuBusy), mgr.request(target(backend), Principal("client", "unix", cls="batch")):
        pass


def test_a_batch_request_waits_while_a_person_uses_the_models(tmp_path):
    mgr, backend, _, _ = make(tmp_path)
    batch = Principal("client", "unix", cls="batch")
    with mgr.request(target(backend), Principal("owner", "tcp")):
        with pytest.raises(GpuBusy), mgr.request(target(backend), batch):
            pass
    with mgr.request(target(backend), batch):
        pass


def test_claim_wait_timeout_proceeds(tmp_path):
    mgr, backend, _, _ = make(tmp_path, coord=FakeCoordinator(held=False))
    with mgr.request(target(backend), OWNER):
        pass


def test_claim_wait_timeout_fails_when_configured(tmp_path):
    mgr, backend, _, _ = make(tmp_path, coord=FakeCoordinator(held=False), on_busy="fail")
    with pytest.raises(GpuBusy), mgr.request(target(backend), OWNER):
        pass


def test_offloaded_ollama_models_are_unloaded_after_a_claim(tmp_path):
    backend = FakeBackend(type_="ollama")
    backend.offloaded = ["big:latest"]
    mgr, *_ = make(tmp_path, backend=backend)
    called = []
    backend.unload_offloaded = lambda: called.append(1) or []
    with mgr.request(target(backend), OWNER):
        pass
    assert called == [1]


def test_v1_idle_timer_unloads_and_releases(tmp_path):
    mgr, backend, coord, clock = make(tmp_path)
    with mgr.request(target(backend, "a"), OWNER):
        pass
    mgr.tick(clock.now + 899)
    assert backend.freed == []
    mgr.tick(clock.now + 901)
    assert backend.freed == [["a"]] and ("release", "hal-gateway") in coord.calls
    mgr.tick(clock.now + 5000)
    assert len(backend.freed) == 1  # nothing was used since


def test_idle_timer_waits_for_requests_in_flight(tmp_path):
    mgr, backend, _, clock = make(tmp_path)
    with mgr.request(target(backend), OWNER):
        mgr.tick(clock.now + 5000)
    assert backend.freed == []


def test_idle_unload_zero_holds_the_models(tmp_path):
    mgr, backend, _, clock = make(tmp_path, idle_unload_s=0.0)
    with mgr.request(target(backend), OWNER):
        pass
    mgr.tick(clock.now + 99999)
    assert backend.freed == []


def test_release_keeps_leased_gpu(tmp_path):
    mgr, backend, _, _ = make(tmp_path)
    mgr.add_lease(LeaseInfo("s1", "coding", "2026-10-30T10:00:00Z"))
    result = mgr.release(force=False, stop_router=False, reclaim=False)
    assert not result.released and result.kept[0].session_id == "s1" and backend.freed == []


def test_force_release_ignores_leases(tmp_path):
    mgr, backend, _, _ = make(tmp_path)
    mgr.add_lease(LeaseInfo("s1", "coding", "2026-10-30T10:00:00Z"))
    assert mgr.release(force=True, stop_router=False, reclaim=False).released
    assert backend.freed


def test_reclaim_ignores_leases_but_is_refused_while_busy(tmp_path):
    mgr, backend, _, _ = make(tmp_path)
    mgr.add_lease(LeaseInfo("s1", "coding", "2026-10-30T10:00:00Z"))
    with mgr.request(target(backend), OWNER):
        busy = mgr.release(force=False, stop_router=False, reclaim=True)
    assert not busy.released and busy.busy == 1 and backend.freed == []
    assert mgr.release(force=False, stop_router=False, reclaim=True).released


def test_release_can_stop_router(tmp_path):
    mgr, *_ = make(tmp_path)
    stopped = []
    mgr.router_stop = lambda: stopped.append(1)
    result = mgr.release(force=False, stop_router=True, reclaim=False)
    assert result.router_stopped and stopped == [1]
    assert result.as_json()["unloaded"] == ["m"]


def test_no_coordinator_still_unloads_when_idle(tmp_path):
    mgr, backend, _, clock = make(tmp_path, coord=None)
    with mgr.request(target(backend), OWNER):
        pass
    mgr.tick(clock.now + 901)
    assert backend.freed


def test_gpu_busy_answers_503_with_retry_after():
    assert GpuBusy("x").status == 503 and GpuBusy("x").retry_after == 30


def test_ollama_frees_only_served_models_and_offloaded_ones(monkeypatch):
    from halgateway.backends import ollama as ollama_mod
    from halgateway.config import load_config  # noqa: F401 - keeps the config tables registered

    posted = []

    def fake_call(backend, method, path, body=None, timeout_s=None):
        if path == "/api/ps":
            return {"models": [
                {"name": "a:latest", "size": 10, "size_vram": 10},
                {"name": "b:latest", "size": 10, "size_vram": 4},
                {"name": "c:latest", "size": 10, "size_vram": 10},
            ]}  # fmt: skip
        posted.append(body["model"])
        return {}

    monkeypatch.setattr(ollama_mod.relay, "call_json", fake_call)
    backend = ollama_mod.OllamaBackend.__new__(ollama_mod.OllamaBackend)
    assert backend.free_vram({"a"}) == ["a:latest"]
    assert backend.unload_offloaded() == ["b:latest"]
    assert posted == ["a:latest", "b:latest"]

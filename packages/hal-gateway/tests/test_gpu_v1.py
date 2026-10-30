"""Tests for the gpu-light protocol 1 file claim and its wait on light.json (REQ-GW-52)."""

import json
import os
import re
import time

import pytest

from halgateway import launchers
from halgateway.gpu import lightv1


@pytest.fixture
def gl_dir(tmp_path):
    path = tmp_path / "gpu-light"
    path.mkdir(mode=0o700)
    return path


def write_light(path, *, state="green", background=(), age_s=0.0, boot=None):
    doc = {
        "protocol": 1, "state": state, "background": list(background),
        "boot_id": boot or launchers.boot_id(), "mono": time.monotonic() - age_s,
        "config": {"stale_after_s": 15},
    }  # fmt: skip
    (path / "light.json").write_text(json.dumps(doc))


def test_v1_claim_file_matches_protocol(gl_dir):
    lightv1.FileClaim(gl_dir, "hal-gateway").write("hal gateway: test")
    doc = json.loads((gl_dir / "claims" / "hal-gateway.json").read_text())
    assert doc["protocol"] == 1 and doc["name"] == "hal-gateway" and doc["lifetime"] == "pid"
    assert doc["pid"] == os.getpid() and doc["pid_start"] == launchers.pid_start(os.getpid())
    assert doc["boot_id"] == launchers.boot_id() and doc["expires"] is None and doc["command"] is None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["created"])
    assert list((gl_dir / "claims").glob(".*")) == []  # no temporary file is left behind


def test_v1_claim_is_removed_on_release(gl_dir):
    coord = lightv1.V1Coordinator("hal-gateway", gl_dir)
    coord.acquire("x", cls="service", wait_s=1, vram_mib=None, uses=[], reason="r")
    assert coord.claim.held()
    coord.release("x")
    assert not coord.claim.held()


def test_claim_in_a_group_readable_directory_is_refused(gl_dir):
    gl_dir.chmod(0o750)
    with pytest.raises(lightv1.ClaimError, match="private directory"):
        lightv1.FileClaim(gl_dir, "hal-gateway").write("r")


def test_claim_name_must_match_the_protocol_pattern(gl_dir):
    with pytest.raises(lightv1.ClaimError, match="must match"):
        lightv1.FileClaim(gl_dir, "Bad Name")


def test_claim_waits_for_background_jobs_then_times_out_with_a_warning(gl_dir):
    write_light(gl_dir, background=[{"pid": 1}])
    clock = [0.0]
    coord = lightv1.V1Coordinator(
        "hal-gateway",
        gl_dir,
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        mono=lambda: clock[0] + time.monotonic(),
    )
    result = coord.acquire("x", cls="service", wait_s=3, vram_mib=None, uses=[], reason="r")
    assert result.held and "background" in result.warning and result.waited_s >= 3


def test_claim_does_not_wait_when_no_watcher_keeps_the_light_fresh(gl_dir):
    write_light(gl_dir, background=[{"pid": 1}], age_s=600)
    sleeps = []
    coord = lightv1.V1Coordinator("hal-gateway", gl_dir, sleep=sleeps.append)
    result = coord.acquire("x", cls="service", wait_s=120, vram_mib=None, uses=[], reason="r")
    assert result.held and result.warning == "" and sleeps == []


def test_light_from_another_boot_reads_unknown(gl_dir):
    write_light(gl_dir, boot="other-boot")
    assert lightv1.effective_state(gl_dir) == "unknown"


def test_state_dir_follows_the_resolution_order(monkeypatch, tmp_path):
    monkeypatch.setenv("GPU_LIGHT_DIR", str(tmp_path / "a"))
    assert lightv1.state_dir() == tmp_path / "a"
    monkeypatch.delenv("GPU_LIGHT_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "x"))
    assert lightv1.state_dir() == tmp_path / "x" / "gpu-light"
    assert lightv1.state_dir("~/custom").name == "custom"

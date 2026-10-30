"""Tests for `halsandbox.detect`: auto order, explicit failures, runsc gating, lockdown gates, cache."""

import json
import os
import stat

import pytest

from halsandbox import detect as dt
from halsandbox.errors import LockdownError, SandboxError
from halsandbox.lockdown import Policy

STANDARD = Policy.for_level("standard")
RELAXED = Policy.for_level("relaxed")
STRICT = Policy.for_level("strict")
OFF = Policy.for_level("off")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HAL_DOCKER", str(tmp_path / "nodocker"))
    monkeypatch.setenv("HAL_BWRAP", str(tmp_path / "nobwrap"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir(mode=0o700)


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def fake_docker(tmp_path, monkeypatch, *, up=True, runtimes=("runc", "runsc"), version="27.0"):
    info = json.dumps({"Runtimes": {r: {} for r in runtimes}, "SecurityOptions": ["name=seccomp"]})
    body = (
        f'case "$1" in version) {"echo " + version if up else "echo daemon down >&2; exit 1"};;\n'
        f"info) echo '{info}';; esac\n"
    )
    _script(tmp_path / "docker", body)
    monkeypatch.setenv("HAL_DOCKER", str(tmp_path / "docker"))


def fake_bwrap(tmp_path, monkeypatch, ok=True):
    _script(tmp_path / "bwrap", "exit 0\n" if ok else "echo no userns >&2; exit 1\n")
    monkeypatch.setenv("HAL_BWRAP", str(tmp_path / "bwrap"))


def probe_ok(docker, image):
    return True, "connected", 4242


def probe_fail(docker, image):
    return False, "cannot connect; register runsc", None


CFG = {"backend": "auto", "runtime": "auto", "require": False}


def test_each_backend_name_is_accepted(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    fake_bwrap(tmp_path, monkeypatch)
    for name, policy in (("docker", STANDARD), ("bwrap", RELAXED), ("none", OFF)):
        got = dt.detect({**CFG, "backend": name}, policy, runsc_probe=probe_ok, refresh=True)
        assert got.backend == name


def test_unknown_backend_name_is_an_error():
    with pytest.raises(SandboxError, match="unknown backend"):
        dt.detect({**CFG, "backend": "podman"}, STANDARD)


def test_auto_prefers_docker_then_bwrap_then_none(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    fake_bwrap(tmp_path, monkeypatch)
    assert dt.detect(CFG, RELAXED, runsc_probe=probe_ok).backend == "docker"
    monkeypatch.setenv("HAL_DOCKER", str(tmp_path / "gone"))
    (tmp_path / "state" / "sandbox" / "last-backend").unlink()
    assert dt.detect(CFG, RELAXED).backend == "bwrap"
    monkeypatch.setenv("HAL_BWRAP", str(tmp_path / "gone"))
    assert dt.detect(CFG, OFF).backend == "none"


def test_docker_down_after_docker_was_used_refuses(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    dt.detect(CFG, STANDARD, runsc_probe=probe_ok)
    fake_docker(tmp_path, monkeypatch, up=False)
    fake_bwrap(tmp_path, monkeypatch)
    with pytest.raises(SandboxError, match="answered before"):
        dt.detect(CFG, RELAXED)
    assert dt.detect({**CFG, "backend": "bwrap"}, RELAXED).backend == "bwrap"  # explicit run is allowed


def test_runsc_used_only_after_probe_passes(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    got = dt.detect(CFG, STANDARD, runsc_probe=probe_ok)
    assert (got.runtime, got.peer_uid) == ("runsc", 4242)
    got = dt.detect(CFG, STANDARD, runsc_probe=probe_fail, refresh=True)
    assert got.runtime == "runc" and "register runsc" in got.detail


def test_explicit_backend_failure_is_an_error(tmp_path, monkeypatch):
    with pytest.raises(SandboxError, match="docker is not installed"):
        dt.detect({**CFG, "backend": "docker"}, STANDARD)
    fake_bwrap(tmp_path, monkeypatch, ok=False)
    with pytest.raises(SandboxError, match="bwrap probe failed"):
        dt.detect({**CFG, "backend": "bwrap"}, RELAXED)


def test_explicit_runsc_with_failing_probe_is_an_error(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    with pytest.raises(SandboxError, match="register runsc"):
        dt.detect({**CFG, "runtime": "runsc"}, STANDARD, runsc_probe=probe_fail)
    fake_docker(tmp_path, monkeypatch, runtimes=("runc",))
    with pytest.raises(SandboxError, match="not registered"):
        dt.detect({**CFG, "runtime": "runsc"}, STANDARD, runsc_probe=probe_ok, refresh=True)


def test_require_refuses_none(tmp_path):
    with pytest.raises(SandboxError, match="require"):
        dt.detect({**CFG, "require": True}, OFF)


def test_strict_refuses_runc_and_standard_refuses_bwrap_and_none(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    with pytest.raises(LockdownError, match="strict"):
        dt.detect(CFG, STRICT, runsc_probe=probe_fail)
    assert dt.detect(CFG, STRICT, runsc_probe=probe_ok, refresh=True).runtime == "runsc"
    fake_bwrap(tmp_path, monkeypatch)
    with pytest.raises(LockdownError, match="relaxed"):
        dt.detect({**CFG, "backend": "bwrap"}, STANDARD)
    with pytest.raises(LockdownError, match="lockdown off"):
        dt.detect({**CFG, "backend": "none"}, RELAXED)


def test_none_is_never_chosen_automatically_below_off(tmp_path):
    with pytest.raises(LockdownError):
        dt.detect(CFG, STANDARD)


def test_probe_cache_expires(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    calls = []

    def counting(docker, image):
        calls.append(1)
        return True, "ok", 1

    dt.detect(CFG, STANDARD, runsc_probe=counting, now=1000.0)
    dt.detect(CFG, STANDARD, runsc_probe=counting, now=1000.0 + 3600)
    assert len(calls) == 1
    dt.detect(CFG, STANDARD, runsc_probe=counting, now=1000.0 + dt.PROBE_TTL_S + 1)
    assert len(calls) == 2
    dt.detect(CFG, STANDARD, runsc_probe=counting, now=1000.0 + dt.PROBE_TTL_S + 2, refresh=True)
    assert len(calls) == 3


def test_a_changed_runtime_list_invalidates_the_cache(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    dt.detect(CFG, STANDARD, runsc_probe=probe_ok)
    fake_docker(tmp_path, monkeypatch, runtimes=("runc",))
    assert dt.detect(CFG, STANDARD, runsc_probe=probe_ok).runtime == "runc"


def test_state_files_are_private(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    dt.detect(CFG, STANDARD, runsc_probe=probe_ok)
    mode = os.stat(tmp_path / "state" / "sandbox" / "probe.json").st_mode & 0o777
    assert mode == 0o600


def test_probe_image_is_never_the_literal_auto():
    """Regression: `[sandbox].image = "auto"` was passed to docker as an image named `auto`."""
    from halsandbox.detect import PROBE_IMAGE, _probe_image

    assert _probe_image({"image": "auto"}) == PROBE_IMAGE
    assert _probe_image({}) == PROBE_IMAGE
    assert _probe_image({"image": "x@sha256:abc"}) == "x@sha256:abc"

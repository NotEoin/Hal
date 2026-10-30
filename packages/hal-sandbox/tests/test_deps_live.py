"""Docker and network checks of the dependency prep box: a tiny `uv.lock` project through the proxy."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from halsandbox import deps
from halsandbox.code import DEFAULT_IMAGE, docker_runtimes

pytestmark = [pytest.mark.docker, pytest.mark.network]

FIXTURE = Path(__file__).parent / "fixtures" / "uvproj"


def runtimes() -> list[str]:
    try:
        have = docker_runtimes()
        inspect = ["docker", "image", "inspect", DEFAULT_IMAGE]
        subprocess.run(inspect, check=True, capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001 - any failure means "no usable docker here", so skip
        return []
    return [r for r in ("runc", "runsc") if r in have]


RUNTIMES = runtimes()
SKIPPED = pytest.param("none", marks=pytest.mark.skip("no docker or image"))


@pytest.fixture
def env(sockdir, tmp_path, monkeypatch):
    os.chmod(sockdir, 0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(sockdir))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[sandbox]\nlockdown = "relaxed"\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))


@pytest.mark.parametrize("runtime", RUNTIMES or [SKIPPED])
def test_uv_sync_through_proxy_and_prep_writes_only_under_work(env, tmp_path, runtime):
    work = tmp_path / "work"
    shutil.copytree(FIXTURE, work)
    work.chmod(0o777)  # the box user is the caller's uid; this keeps the test portable to rootless docker
    (result,) = deps.prepare(work, approve=lambda lf: True, runtime=runtime, timeout_s=300)
    assert result.ok, result.log_tail
    assert list(work.glob(".hal/venv/lib/python3*/site-packages/six.py"))
    assert sorted(p.name for p in work.iterdir()) == [".hal", "pyproject.toml", "uv.lock"]
    assert deps.pending(work) == []

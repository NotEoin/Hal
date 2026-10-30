"""Tests for per-project scratchpads and resource limits."""

import pytest

from halsandbox import scratch
from halsandbox.errors import SandboxError
from halsandbox.limits import Limits, parse_size


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "state"))


def test_project_key_is_stable_and_differs_per_path(tmp_path):
    a, b = tmp_path / "x" / "app", tmp_path / "y" / "app"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert scratch.project_key(a) == scratch.project_key(a)
    assert scratch.project_key(a) != scratch.project_key(b)
    assert scratch.project_key(a).startswith("app-")


def test_prepare_makes_a_private_dir_under_state(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    d = scratch.prepare(proj, 1000)
    assert d.parent == tmp_path / "state" / "scratch" and d.stat().st_mode & 0o777 == 0o700


def test_prepare_refuses_an_over_cap_scratchpad(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (scratch.prepare(proj, 1000) / "big").write_bytes(b"x" * 2000)
    with pytest.raises(SandboxError, match="hal scratch clean"):
        scratch.prepare(proj, 1000)


def test_clean_removes_one_project_or_all(tmp_path):
    p1, p2 = tmp_path / "a", tmp_path / "b"
    p1.mkdir()
    p2.mkdir()
    scratch.prepare(p1, 10)
    scratch.prepare(p2, 10)
    assert len(scratch.clean(p1)) == 1 and len(scratch.clean()) == 1 and scratch.clean() == []


def test_tmp_size_must_stay_below_memory():
    with pytest.raises(ValueError, match="tmpfs"):
        Limits.from_config({"memory": "512m", "tmp_size": "1g"})


def test_auto_limits_are_capped():
    lim = Limits.from_config({})
    assert lim.memory_bytes <= 8 * 1024**3 and 1 <= lim.cpus <= 4


def test_bad_size_is_rejected():
    with pytest.raises(ValueError):
        parse_size("lots")

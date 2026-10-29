"""Tests for `halkit.testing.home`."""

import os
import stat

from halkit import paths
from halkit.testing import TempHalHome


def test_paths_resolve_into_the_temp_home_inside_the_block():
    with TempHalHome() as home:
        assert paths.config_file() == home.config
        assert paths.policy_file() == home.policy
        assert paths.state_dir() == home.state
        assert paths.data_dir() == home.data
        assert paths.runtime_dir() == home.runtime


def test_runtime_dir_is_private_and_leaves_room_for_a_socket_path():
    with TempHalHome() as home:
        assert stat.S_IMODE(home.runtime.stat().st_mode) == 0o700
        paths.check_socket_path(paths.session_dir("0123456789ab") / "run" / "broker.sock")


def test_environment_is_restored_and_the_directory_removed_on_exit(monkeypatch):
    monkeypatch.setenv("HAL_CONFIG", "/before/config.toml")
    monkeypatch.delenv("HAL_STATE_DIR", raising=False)
    with TempHalHome() as home:
        root = home.root
    assert os.environ["HAL_CONFIG"] == "/before/config.toml"
    assert "HAL_STATE_DIR" not in os.environ
    assert not root.exists()


def test_deprecated_aliases_and_xdg_variables_do_not_leak_in(monkeypatch):
    monkeypatch.setenv("HAL_STATE", "/old/state")
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    with TempHalHome() as home:
        assert paths.state_dir() == home.state
    assert os.environ["HAL_STATE"] == "/old/state"


def test_environment_is_restored_when_the_block_raises():
    before = os.environ.get("HAL_CONFIG")
    try:
        with TempHalHome():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert os.environ.get("HAL_CONFIG") == before

"""Tests for `halkit.paths`."""

import os
import stat
from pathlib import Path

import pytest

from halkit import paths

HAL_ENV = (
    "HAL_CONFIG",
    "HAL_POLICY",
    "HAL_STATE_DIR",
    "HAL_DATA_DIR",
    "HAL_RUNTIME_DIR",
    "HAL_STATE",
    "HAL_DATA",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "LOCALAPPDATA",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in HAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(paths, "RUN_USER_ROOT", tmp_path / "run-user")
    monkeypatch.setattr(paths, "_warned", set())


def test_config_file_defaults_to_xdg_config_under_home(tmp_path):
    assert paths.config_file() == tmp_path / "home/.config/hal/config.toml"


def test_config_file_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths.config_file() == tmp_path / "xdg/hal/config.toml"


def test_empty_xdg_variable_counts_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert paths.config_file() == tmp_path / "home/.config/hal/config.toml"


def test_hal_config_overrides_the_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "x.toml"))
    assert paths.config_file() == tmp_path / "x.toml"
    assert paths.config_dir() == tmp_path


def test_policy_file_sits_beside_the_config_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "c/config.toml"))
    assert paths.policy_file() == tmp_path / "c/policy.toml"


def test_hal_policy_overrides_the_policy_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_POLICY", str(tmp_path / "p.toml"))
    assert paths.policy_file() == tmp_path / "p.toml"


def test_state_and_data_dirs_follow_xdg_defaults(tmp_path):
    assert paths.state_dir() == tmp_path / "home/.local/state/hal"
    assert paths.data_dir() == tmp_path / "home/.local/share/hal"


def test_state_and_data_dirs_follow_xdg_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    assert paths.state_dir() == tmp_path / "s/hal"
    assert paths.data_dir() == tmp_path / "d/hal"


def test_runtime_dir_uses_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xrd"))
    assert paths.runtime_dir() == tmp_path / "xrd/hal"


def test_runtime_dir_falls_back_to_run_user_uid(tmp_path):
    (tmp_path / "run-user" / str(os.getuid())).mkdir(parents=True)
    assert paths.runtime_dir() == tmp_path / "run-user" / str(os.getuid()) / "hal"


def test_runtime_dir_missing_names_the_fix_and_never_uses_tmp():
    with pytest.raises(paths.RuntimeDirMissing, match="HAL_RUNTIME_DIR"):
        paths.runtime_dir()


def test_runtime_dir_on_windows_uses_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.runtime_dir() == tmp_path / "local" / "hal" / "run"


def test_runtime_dir_on_windows_without_local_app_data_is_missing(monkeypatch):
    monkeypatch.setattr(paths, "_is_windows", lambda: True)
    with pytest.raises(paths.RuntimeDirMissing):
        paths.runtime_dir()


def test_runtime_dir_import_does_not_read_the_uid(monkeypatch):
    def boom() -> int:
        raise AssertionError("uid read at import")

    monkeypatch.setattr(paths, "_uid", boom)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent")
    assert paths.runtime_dir() == Path("/nonexistent/hal")


def test_existing_runtime_dir_with_group_access_is_unsafe(monkeypatch, tmp_path):
    runtime = tmp_path / "xrd/hal"
    runtime.mkdir(parents=True)
    runtime.chmod(0o750)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xrd"))
    with pytest.raises(paths.UnsafePathError, match="0750"):
        paths.runtime_dir()


def test_existing_runtime_dir_owned_by_another_user_is_unsafe(monkeypatch, tmp_path):
    runtime = tmp_path / "xrd/hal"
    runtime.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xrd"))
    monkeypatch.setattr(paths, "_uid", lambda: os.getuid() + 1)
    with pytest.raises(paths.UnsafePathError, match="owned by uid"):
        paths.runtime_dir()


def test_session_dir_is_under_s_in_the_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xrd"))
    assert paths.session_dir("0123456789ab") == tmp_path / "xrd/hal/s/0123456789ab"


@pytest.mark.parametrize("bad", ["", "0123456789A", "0123456789abc", "../../etc/x", "0123456789ag"])
def test_session_dir_rejects_a_malformed_id(bad):
    with pytest.raises(ValueError, match="session id"):
        paths.session_dir(bad)


def test_ensure_private_dir_creates_mode_0700(tmp_path):
    made = paths.ensure_private_dir(tmp_path / "a" / "b")
    assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_ensure_private_dir_accepts_an_existing_private_dir(tmp_path):
    (tmp_path / "d").mkdir(mode=0o700)
    assert paths.ensure_private_dir(tmp_path / "d") == tmp_path / "d"


def test_ensure_private_dir_refuses_an_open_existing_dir(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d").chmod(0o755)
    with pytest.raises(paths.UnsafePathError):
        paths.ensure_private_dir(tmp_path / "d")
    assert stat.S_IMODE((tmp_path / "d").stat().st_mode) == 0o755  # never loosened or repaired


def test_socket_path_at_the_limit_is_accepted():
    path = Path("/" + "a" * (paths.MAX_SOCKET_PATH_BYTES - 1))
    assert paths.check_socket_path(path) == path


def test_socket_path_over_107_bytes_is_refused():
    with pytest.raises(paths.SocketPathTooLong, match="108 bytes"):
        paths.check_socket_path(Path("/" + "a" * paths.MAX_SOCKET_PATH_BYTES))


def test_a_session_socket_path_is_short_enough():
    sock = Path("/run/user/1000/hal/s/0123456789ab/run/broker.sock")
    assert len(os.fsencode(sock)) < 60
    assert paths.check_socket_path(sock) == sock


def test_hal_state_dir_overrides_the_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "st"))
    assert paths.state_dir() == tmp_path / "st"


def test_hal_data_dir_overrides_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_DATA_DIR", str(tmp_path / "da"))
    assert paths.data_dir() == tmp_path / "da"


def test_hal_runtime_dir_wins_over_xdg_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xrd"))
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(tmp_path / "rt"))
    assert paths.runtime_dir() == tmp_path / "rt"


def test_two_installs_with_different_state_dirs_share_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "one"))
    first = paths.state_dir()
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "two"))
    assert first != paths.state_dir()


def test_deprecated_hal_state_is_honoured_with_one_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "old"))
    with caplog.at_level("WARNING", logger="halkit.paths"):
        assert paths.state_dir() == tmp_path / "old"
        assert paths.state_dir() == tmp_path / "old"
    assert [r.getMessage() for r in caplog.records] == ["HAL_STATE is deprecated; use HAL_STATE_DIR"]


def test_deprecated_hal_data_is_honoured_with_one_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HAL_DATA", str(tmp_path / "old"))
    with caplog.at_level("WARNING", logger="halkit.paths"):
        assert paths.data_dir() == tmp_path / "old"
        paths.data_dir()
    assert len(caplog.records) == 1


def test_new_variable_beats_the_deprecated_one_without_a_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "old"))
    monkeypatch.setenv("HAL_STATE_DIR", str(tmp_path / "new"))
    with caplog.at_level("WARNING", logger="halkit.paths"):
        assert paths.state_dir() == tmp_path / "new"
    assert not caplog.records

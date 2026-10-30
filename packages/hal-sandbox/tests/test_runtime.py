"""Tests for `halsandbox.runtime`: directory layout and modes, owner.json, generated /etc files."""

import json
import os
import stat

import pytest

from halkit import paths
from halsandbox import runtime
from halsandbox.errors import SandboxError


@pytest.fixture
def rdir(sockdir, monkeypatch):
    """A private runtime root."""
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(sockdir))
    os.chmod(sockdir, 0o700)
    return sockdir


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_runtime_dir_layout_and_modes(rdir):
    rd = runtime.RuntimeDir.create("0123456789ab")
    assert rd.root == paths.session_dir("0123456789ab")
    assert mode(rd.root) == 0o700 and mode(rd.run) == 0o700
    assert mode(rd.etc / "passwd") == 0o644 and mode(rd.empty) == 0o444
    assert rd.empty.stat().st_size == 0


def test_socket_paths_fit_the_unix_limit(rdir):
    rd = runtime.RuntimeDir.create()
    for name in runtime.SOCKETS:
        assert len(os.fsencode(rd.socket(name))) <= paths.MAX_SOCKET_PATH_BYTES
    with pytest.raises(ValueError):
        rd.socket("evil.sock")


def test_too_long_runtime_root_fails_before_anything_is_created(rdir, monkeypatch):
    long = rdir / ("d" * 90)
    long.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(long))
    with pytest.raises(paths.SocketPathTooLong):
        runtime.RuntimeDir.create("0123456789ab")
    assert not (long / "s").exists()


def test_existing_session_dir_is_refused(rdir):
    runtime.RuntimeDir.create("0123456789ab")
    with pytest.raises(SandboxError):
        runtime.RuntimeDir.create("0123456789ab")


def test_owner_json_has_every_key_and_a_live_pidstart(rdir):
    rd = runtime.RuntimeDir.create()
    rd.write_owner(kind="code", backend="docker", container="hal-code-x", work="/p")
    data = json.loads((rd.root / "owner.json").read_text())
    assert tuple(data) == runtime.OWNER_KEYS
    assert data["pid"] == os.getpid() and data["pidstart"] == runtime.pidstart(os.getpid()) > 0
    assert data["created"].endswith("Z") and mode(rd.root / "owner.json") == 0o600


def test_passwd_and_group_name_hal_with_the_host_ids(rdir):
    rd = runtime.RuntimeDir.create(uid=1234, gid=5678)
    assert "hal:x:1234:5678:" in (rd.etc / "passwd").read_text()
    assert "hal:x:5678:" in (rd.etc / "group").read_text()


def test_session_json_schema(rdir):
    rd = runtime.RuntimeDir.create("0123456789ab")
    data = rd.write_session_json({"backend": "docker", "masked": [".env"]})
    on_disk = json.loads((rd.run / "session.json").read_text())
    assert on_disk == data and data["schema"] == 1 and data["workdir"] == "/work"
    assert data["sockets"]["broker"] == "/run/hal/broker.sock"
    assert mode(rd.run / "session.json") == 0o644


def test_remove_deletes_the_tree(rdir):
    rd = runtime.RuntimeDir.create()
    rd.remove()
    assert not rd.root.exists()
    rd.remove()


def test_new_session_id_is_12_hex():
    assert paths.SESSION_ID_RE.fullmatch(runtime.new_session_id())


def test_read_cid_accepts_only_a_hex_container_id(tmp_path):
    from halsandbox.runtime import read_cid

    assert read_cid(tmp_path) is None
    (tmp_path / "cid").write_text("not an id\n")
    assert read_cid(tmp_path) is None
    (tmp_path / "cid").write_text("0123456789abcdef" * 4 + "\n")
    assert read_cid(tmp_path) == "0123456789abcdef" * 4

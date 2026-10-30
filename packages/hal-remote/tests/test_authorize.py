"""Tests for authorize, revoke, the registry and token delivery (REQ-RMT-06 to 13, 46)."""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from halremote import authkeys, authorize, serve
from halremote.authorize import AuthorizeError, AuthorizeRequest

from .conftest import FakeRunner
from .test_servercheck import GOOD_SSHD, SS_OK


def _blob(fill: bytes) -> str:
    wire = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + fill * 32
    return base64.b64encode(wire).decode("ascii")


BLOB_T = _blob(b"\x01")
BLOB_M = _blob(b"\x02")
TUNNEL = f"ssh-ed25519 {BLOB_T} hal-remote:laptop:tunnel"
MCP = f"ssh-ed25519 {BLOB_M} hal-remote:laptop:mcp"
SETTINGS = {"delivery_ttl_s": 86400, "from_prefix_v4": 24, "from_prefix_v6": 64}


class FakeHal:
    """Stands in for the `hal` program and, for other argv, delegates to a `FakeRunner`."""

    def __init__(self, hal: Path, base: FakeRunner) -> None:
        self.hal = str(hal)
        self.base = base
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()  # subcommand words that exit 1
        self.tokens = 0

    def __call__(self, argv, *, timeout, check=True, **kw):
        if argv[0] != self.hal:
            return self.base(argv, timeout=timeout, check=check, **kw)
        self.calls.append(list(argv[1:]))
        assert timeout > 0
        key = " ".join(argv[1:4])
        if key in self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"boom\n")
        if argv[1:4] == ["gateway", "token", "issue"]:
            self.tokens += 1
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"name": "n", "token": f"tok-{self.tokens}"}).encode(), b""
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def count(self, prefix: list[str]) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == prefix)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A passing desktop plus fake hal, hub, serve and state directories."""
    for key in list(os.environ):
        if key.startswith("HAL_"):
            monkeypatch.delenv(key)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_STATE_DIR", str(state))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "sshd_config").write_text(GOOD_SSHD)
    (etc / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 HOSTKEY host\n")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    os.chmod(home, 0o755)
    os.chmod(home / ".ssh", 0o700)
    akeys = home / ".ssh" / "authorized_keys"
    akeys.write_text("ssh-ed25519 FOREIGN me@host\n")
    akeys.chmod(0o600)
    exes = {}
    for name in ("hal", "hal-hub", "hal-remote", "sshd"):
        exe = tmp_path / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        exes[name] = exe
    base = FakeRunner(
        {"systemctl": (0, "active\n"), "ss": (0, SS_OK), "ufw": None, sys.executable: (0, '{"ok": true}\n')}
    )
    hal = FakeHal(exes["hal"], base)
    resolved = authorize.Resolved(exes["hal-remote"], exes["hal-hub"], exes["hal"])
    return {
        "hal": hal, "akeys": akeys, "resolved": resolved, "home": home, "etc": etc, "tmp": tmp_path,
        "options": {
            "user": "alice", "uid": os.getuid(), "sshd_path": exes["sshd"],
            "http_status": lambda url, timeout: 401, "now": 1_000_000.0,
        },
    }  # fmt: skip


def go(desk, *, req=None, addr="192.168.1.20", risks=frozenset(), **over):
    req = req or AuthorizeRequest.build(client="laptop", tunnel_key=TUNNEL, mcp_key=MCP)
    return authorize.authorize(
        req,
        client_addr=addr,
        runner=desk["hal"],
        accept_risks=risks,
        authorized_keys=desk["akeys"],
        resolved=desk["resolved"],
        etc_ssh=desk["etc"],
        home=desk["home"],
        settings=SETTINGS,
        check_options=desk["options"],
        **over,
    )


def test_authorize_writes_two_restricted_lines_and_keeps_foreign_lines(desk):
    resp = go(desk)
    text = desk["akeys"].read_text()
    assert text.startswith("ssh-ed25519 FOREIGN me@host\n")
    owned, other = authkeys.parse(text)
    assert sorted(ln.role for ln in owned) == ["mcp", "tunnel"]
    assert other == 1
    assert 'from="192.168.1.0/24"' in text
    assert resp["from"] == "192.168.1.0/24"
    assert resp["host_keys"] == ["ssh-ed25519 HOSTKEY host"]
    assert oct(desk["akeys"].stat().st_mode & 0o777) == "0o600"


def test_authorize_holds_the_token_for_delivery_in_a_private_file_and_never_returns_it(desk):
    resp = go(desk)
    assert "tok-1" not in json.dumps(resp)
    delivery = serve.delivery_dir() / "laptop.json"
    assert json.loads(delivery.read_text())["tokens"] == {"gateway": "tok-1"}
    assert oct(delivery.stat().st_mode & 0o777) == "0o600"
    assert desk["hal"].calls[-1][:4] == ["gateway", "token", "issue", "--name"]
    assert desk["hal"].calls[-1][4] == "remote-laptop"


def test_authorize_records_the_client_without_secrets(desk):
    go(desk)
    (rec,) = authorize.clients()
    assert rec.name == "laptop"
    assert rec.gateway_token_name == "remote-laptop"
    assert rec.forwards == (8741,)
    raw = authorize.registry_path().read_text()
    assert "tok-1" not in raw
    assert rec.tunnel_fingerprint.startswith("SHA256:")
    assert oct(authorize.registry_path().stat().st_mode & 0o777) == "0o600"


def test_authorize_turns_the_gateway_listener_on_only_when_it_is_off(desk, tmp_path):
    assert go(desk)["gateway_tcp_enabled"] is True
    assert desk["hal"].count(["config", "set", "gateway.tcp", "true"]) == 1
    (tmp_path / "config.toml").write_text("[gateway]\ntcp = true\n")
    req = AuthorizeRequest.build(
        client="second",
        tunnel_key=TUNNEL.replace("laptop", "second"),
        mcp_key=MCP.replace("laptop", "second"),
    )
    assert go(desk, req=req)["gateway_tcp_enabled"] is False
    assert desk["hal"].count(["config", "set", "gateway.tcp", "true"]) == 1


def test_a_failed_critical_check_refuses_and_changes_nothing(desk):
    (desk["etc"] / "sshd_config").write_text("PasswordAuthentication yes\n")
    before = desk["akeys"].read_text()
    with pytest.raises(AuthorizeError, match="SC03"):
        go(desk)
    assert desk["akeys"].read_text() == before
    assert desk["hal"].calls == []
    assert authorize.clients() == []


def test_accept_risk_lets_a_failed_critical_check_pass_and_is_recorded(desk):
    (desk["etc"] / "sshd_config").write_text(
        GOOD_SSHD.replace("PasswordAuthentication no", "PasswordAuthentication yes")
    )
    go(desk, risks=frozenset({"SC03"}))
    assert authorize.clients()[0].accepted_risks == ("SC03",)


def test_sc07_and_sc08_warn_but_do_not_block(desk):
    (desk["etc"] / "sshd_config").write_text(
        GOOD_SSHD.replace("AllowTcpForwarding local\n", "").replace("AllowStreamLocalForwarding no\n", "")
    )
    resp = go(desk)
    assert {"SC07", "SC08"} <= set(resp["checks"]["warnings"])


def test_gateway_listener_not_yet_on_does_not_block_the_first_authorize(desk):
    desk["options"]["http_status"] = lambda url, timeout: None
    assert go(desk)["token_pending"] is True


def test_token_failure_rolls_back_the_lines_and_the_listener(desk):
    desk["hal"].fail.add("gateway token issue")
    before = desk["akeys"].read_text()
    with pytest.raises(AuthorizeError, match="token"):
        go(desk)
    assert desk["akeys"].read_text() == before
    assert desk["hal"].count(["config", "set", "gateway.tcp", "false"]) == 1
    assert authorize.clients() == []
    assert not (serve.delivery_dir() / "laptop.json").exists()


def test_listener_failure_rolls_back_the_lines(desk):
    desk["hal"].fail.add("config set gateway.tcp")
    before = desk["akeys"].read_text()
    with pytest.raises(AuthorizeError):
        go(desk)
    assert desk["akeys"].read_text() == before
    assert desk["hal"].tokens == 0


def test_registry_failure_rolls_back_lines_token_and_delivery(desk, monkeypatch):
    monkeypatch.setattr(
        authorize, "_save_registry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    before = desk["akeys"].read_text()
    with pytest.raises(AuthorizeError, match="rolled back"):
        go(desk)
    assert desk["akeys"].read_text() == before
    assert desk["hal"].count(["gateway", "token", "revoke"]) == 1
    assert not (serve.delivery_dir() / "laptop.json").exists()


def test_an_existing_client_needs_rotate(desk):
    go(desk)
    with pytest.raises(AuthorizeError, match="--rotate"):
        go(desk)
    req = AuthorizeRequest.build(client="laptop", tunnel_key=TUNNEL, mcp_key=MCP, rotate=True)
    go(desk, req=req)
    owned, _ = authkeys.parse(desk["akeys"].read_text())
    assert len(owned) == 2
    assert desk["hal"].calls[-1][-1] == "--rotate"


def test_public_client_address_needs_an_explicit_from(desk):
    with pytest.raises(AuthorizeError, match="from"):
        go(desk, addr="8.8.8.8")
    with pytest.raises(AuthorizeError, match="--from"):
        go(desk, addr=None)


def test_from_any_omits_the_option_and_is_recorded(desk):
    req = AuthorizeRequest.build(client="laptop", tunnel_key=TUNNEL, mcp_key=MCP, from_pattern="any")
    go(desk, req=req)
    assert "from=" not in desk["akeys"].read_text()
    assert authorize.clients()[0].from_pattern == "any"


def test_expires_adds_expiry_time_to_both_lines(desk):
    req = AuthorizeRequest.build(client="laptop", tunnel_key=TUNNEL, mcp_key=MCP, expires="2027-01-31")
    go(desk, req=req)
    assert desk["akeys"].read_text().count('expiry-time="20270131Z"') == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"client": "Bad Name"},
        {"tunnel_key": "not a key"},
        {"mcp_key": TUNNEL},
        {"expires": "tomorrow"},
        {"rotate": "yes"},
    ],
)
def test_request_validation_rejects_bad_fields(bad):
    fields = {"client": "laptop", "tunnel_key": TUNNEL, "mcp_key": MCP, **bad}
    with pytest.raises(AuthorizeError):
        AuthorizeRequest.build(**fields)


def test_request_json_needs_the_schema():
    with pytest.raises(AuthorizeError, match="schema"):
        AuthorizeRequest.from_json("{}")
    body = {"schema": authorize.SCHEMA_REQUEST, "client": "laptop", "tunnel_key": TUNNEL, "mcp_key": MCP}
    assert AuthorizeRequest.from_json(json.dumps(body)).client == "laptop"


@pytest.mark.parametrize("name", ['a"b', "a\\b", "a\nb"])
def test_paths_with_forbidden_characters_are_refused(tmp_path, name):
    with pytest.raises(AuthorizeError):
        authorize._resolve_exe(str(tmp_path / name), "hal-hub")


def test_relative_or_missing_executables_are_refused(tmp_path):
    with pytest.raises(AuthorizeError):
        authorize._resolve_exe("relative/hub", "hal-hub")
    with pytest.raises(AuthorizeError):
        authorize._resolve_exe(str(tmp_path / "missing"), "hal-hub")


def test_revoke_removes_lines_registry_delivery_and_token(desk):
    go(desk)
    assert (
        authorize.revoke(
            "laptop", runner=desk["hal"], authorized_keys=desk["akeys"], resolved=desk["resolved"]
        )
        == []
    )
    assert desk["akeys"].read_text() == "ssh-ed25519 FOREIGN me@host\n"
    assert authorize.clients() == []
    assert not (serve.delivery_dir() / "laptop.json").exists()
    assert desk["hal"].calls[-1] == ["gateway", "token", "revoke", "--name", "remote-laptop"]


def test_revoke_names_what_it_could_not_remove(desk):
    go(desk)
    desk["hal"].fail.add("gateway token revoke")
    problems = authorize.revoke(
        "laptop", runner=desk["hal"], authorized_keys=desk["akeys"], resolved=desk["resolved"]
    )
    assert len(problems) == 1
    assert problems[0].startswith("gateway token")


def test_rotate_token_issues_a_new_token_for_delivery(desk):
    go(desk)
    (serve.delivery_dir() / "laptop.json").unlink()
    token = authorize.rotate_token("laptop", runner=desk["hal"], resolved=desk["resolved"])
    assert token == "tok-2"
    assert json.loads((serve.delivery_dir() / "laptop.json").read_text())["tokens"] == {"gateway": "tok-2"}
    with pytest.raises(AuthorizeError, match="no client"):
        authorize.rotate_token("ghost", runner=desk["hal"], resolved=desk["resolved"])


def test_update_from_changes_only_the_range(desk):
    go(desk)
    authorize.update_from("laptop", "10.0.0.0/8", authorized_keys=desk["akeys"], resolved=desk["resolved"])
    text = desk["akeys"].read_text()
    assert text.count('from="10.0.0.0/8"') == 2
    assert "192.168.1.0/24" not in text
    assert authorize.clients()[0].from_pattern == "10.0.0.0/8"
    assert "FOREIGN" in text

"""Tests for `hal remote setup`: bootstrap and manual pairing with scripted ssh programs."""

from __future__ import annotations

import base64
import json
import struct
import subprocess
from pathlib import Path

import pytest

from halremote import authorize, pairing, sshconf
from halremote.keys import parse_public_key
from halremote.model import profile_paths


def _blob(fill: int) -> str:
    wire = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([fill]) * 32
    return base64.b64encode(wire).decode("ascii")


HOST_BLOB = _blob(9)
HOST_KEY = f"ssh-ed25519 {HOST_BLOB}"
HOST_FP = parse_public_key(HOST_KEY).fingerprint()
SSH = Path("/fake/bin/ssh")


class Scripted:
    """A runner that plays ssh-keygen, ssh and ssh-keyscan for the desktop."""

    def __init__(self, *, hello_tokens: dict | None = None, authorize_exit: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[bytes | None] = []
        self.tokens = {"gateway": "remote-secret-token"} if hello_tokens is None else hello_tokens
        self.authorize_exit = authorize_exit
        self.keys = 0

    def __call__(self, argv, *, timeout, check=True, input=None, **_kw):
        assert timeout > 0
        self.calls.append(list(argv))
        self.inputs.append(input)
        name = Path(argv[0]).name
        if name == "ssh-keygen":
            self.keys += 1
            target = Path(argv[argv.index("-f") + 1])
            target.write_text("PRIVATE", encoding="ascii")
            comment = argv[argv.index("-C") + 1]
            target.with_name(target.name + ".pub").write_text(f"ssh-ed25519 {_blob(self.keys)} {comment}\n")
            return _done(argv, 0, "")
        if name == "ssh-keyscan":
            return _done(argv, 0, f"desk.lan ssh-ed25519 {HOST_BLOB}\n")
        if argv[-1] == "hello":
            body = {"schema": "hal.remote.hello/1", "tokens": self.tokens}
            return _done(argv, 0, json.dumps(body))
        resp = {"schema": authorize.SCHEMA_RESPONSE, "host_keys": [HOST_KEY]}
        return _done(argv, self.authorize_exit, json.dumps(resp), "denied" if self.authorize_exit else "")


def _done(argv, code, out, err=""):
    return subprocess.CompletedProcess(argv, code, out.encode(), err.encode())


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(var, str(tmp_path / var))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(sshconf, "find_ssh", lambda override="": SSH)
    monkeypatch.setattr(sshconf, "find_ssh_keygen", lambda ssh: Path("/fake/bin/ssh-keygen"))
    monkeypatch.setattr(sshconf, "find_ssh_keyscan", lambda ssh: Path("/fake/bin/ssh-keyscan"))
    return tmp_path


def _opts(**kw):
    return pairing.SetupOptions(host="me@desk.lan", client="laptop", **kw)


def test_bootstrap_sends_public_keys_only_and_pins_the_host_key(home):
    runner = Scripted()
    pairing.setup(_opts(), runner=runner)
    sent = next(i for i in runner.inputs if i)
    assert b"PRIVATE" not in sent
    request = json.loads(sent)
    assert request["client"] == "laptop" and request["tunnel_key"].startswith("ssh-ed25519 ")
    pp = profile_paths("desktop")
    assert pp.known_hosts.read_text().strip() == f"hal-desktop ssh-ed25519 {HOST_BLOB}"


def test_bootstrap_runs_the_remote_command_with_an_unexpanded_tilde(home):
    runner = Scripted()
    pairing.setup(_opts(), runner=runner)
    ssh_call = next(c for c in runner.calls if c[0] == str(SSH) and c[-1] != "hello")
    assert ssh_call[-1].startswith("~/.local/bin/hal-remote authorize --client laptop --stdin --json")
    assert ssh_call[ssh_call.index("--") + 1] == "desk.lan"


def test_setup_stores_the_delivered_token_with_private_mode(home):
    pairing.setup(_opts(), runner=Scripted())
    pp = profile_paths("desktop")
    assert pp.token.read_text().strip() == "remote-secret-token"
    assert pp.token.stat().st_mode & 0o777 == 0o600


def test_setup_writes_a_profile_that_loads_back(home):
    pairing.setup(_opts(port=2222), runner=Scripted())
    profile = pairing.load_profile("desktop")
    assert (profile.host, profile.user, profile.port, profile.client) == ("desk.lan", "me", 2222, "laptop")
    assert profile.forwards[0].local_port == 8741
    assert profile_paths("desktop").ssh_config.read_text().count("Host hal-desktop-tunnel") == 2


def test_setup_appends_gateway_client_mode_and_desktop_tools_once(home):
    result = pairing.setup(_opts(), runner=Scripted())
    text = (home / "config.toml").read_text()
    assert result.gateway_written and 'url = "http://127.0.0.1:8741"' in text
    assert "[mcp.servers.hal-desktop]\nhost = true" in text
    assert str(profile_paths("desktop").token.as_posix()) in text


def test_setup_leaves_an_existing_gateway_table_and_prints_the_lines(home):
    (home / "config.toml").write_text("[gateway]\ntcp = false\n")
    result = pairing.setup(_opts(), runner=Scripted())
    assert not result.gateway_written and "token_file" in result.gateway_lines
    assert (home / "config.toml").read_text().count("[gateway]") == 1


def test_setup_skips_desktop_tools_when_disabled(home):
    result = pairing.setup(_opts(desktop_tools=False), runner=Scripted())
    assert result.mcp_server_lines == ""
    assert "mcp.servers" not in (home / "config.toml").read_text()


def test_failed_authorize_reports_the_remote_error_and_writes_no_token(home):
    with pytest.raises(pairing.PairingError, match="denied"):
        pairing.setup(_opts(), runner=Scripted(authorize_exit=1))
    assert not profile_paths("desktop").token.exists()


def test_already_delivered_token_gives_a_rotate_hint(home):
    with pytest.raises(pairing.PairingError, match="rotate-token"):
        pairing.setup(_opts(), runner=Scripted(hello_tokens={}))


def test_second_setup_without_rotate_is_refused(home):
    pairing.setup(_opts(), runner=Scripted())
    with pytest.raises(pairing.PairingError, match="--rotate"):
        pairing.setup(_opts(), runner=Scripted())


def test_rotate_replaces_the_keys(home):
    pairing.setup(_opts(), runner=Scripted())
    runner = Scripted()
    pairing.setup(_opts(rotate=True, desktop_tools=False), runner=runner)
    assert runner.keys == 2


def test_manual_flow_pins_the_key_when_the_user_confirms(home):
    shown: list[str] = []
    pairing.setup(
        _opts(manual=True, from_pattern="192.168.1.0/24"),
        runner=Scripted(),
        confirm=lambda question: HOST_FP in question,
        wait_for_desktop=shown.append,
    )
    assert shown[0].startswith("hal remote authorize --client laptop --tunnel-key 'ssh-ed25519 ")
    assert "--from 192.168.1.0/24" in shown[0]
    assert HOST_BLOB in profile_paths("desktop").known_hosts.read_text()


def test_manual_flow_refuses_when_the_user_says_no(home):
    with pytest.raises(pairing.PairingError, match="not confirmed"):
        pairing.setup(
            _opts(manual=True), runner=Scripted(), confirm=lambda q: False, wait_for_desktop=lambda c: None
        )
    assert not profile_paths("desktop").known_hosts.exists()


def test_manual_flow_with_a_wrong_fingerprint_is_a_mismatch(home):
    wrong = "SHA256:" + "A" * 43
    with pytest.raises(pairing.PairingError, match="mismatch"):
        pairing.setup(
            _opts(manual=True, host_key_fingerprint=wrong),
            runner=Scripted(),
            confirm=lambda q: True,
            wait_for_desktop=lambda c: None,
        )


def test_manual_flow_accepts_a_matching_fingerprint_without_asking(home):
    pairing.setup(
        _opts(manual=True, host_key_fingerprint=HOST_FP),
        runner=Scripted(),
        confirm=lambda q: pytest.fail("must not ask"),
        wait_for_desktop=lambda c: None,
    )


@pytest.mark.parametrize("target", ["desk.lan", "-oProxyCommand=x@h", "me@", "a b@h"])
def test_bad_targets_are_rejected(target):
    with pytest.raises(pairing.PairingError):
        pairing.split_target(target, "")


def test_profile_text_round_trips():
    profile = pairing.parse_profile(
        pairing.render_profile(
            pairing.Profile("desktop", "laptop", "h", "u", 22, (pairing.Forward("gateway", 8741, 8741),))
        )
    )
    assert profile.forwards[0].remote_port == 8741 and profile.tunnel.backoff_max_s == 60

"""Tests for `halremote.authkeys`: golden lines, quoting, merge, remove, parse and write."""

import os
import stat
import threading
from datetime import date
from pathlib import Path

import pytest

from halremote import authkeys
from halremote.keys import PublicKey
from halremote.model import Forward

BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
KEY = PublicKey("ssh-ed25519", BLOB, "ignored")
FORWARDS = (Forward("gateway", 18741, 8741),)
SERVE = Path("/home/u/.local/bin/hal-remote")
HUB = Path("/home/u/.local/bin/hal-hub")

TUNNEL_GOLDEN = (
    'restrict,port-forwarding,from="192.168.1.0/24",permitopen="127.0.0.1:8741",'
    'permitlisten="127.0.0.1:1",command="/bin/false" ssh-ed25519 ' + BLOB + " hal-remote:laptop:tunnel"
)
MCP_GOLDEN = (
    'restrict,from="192.168.1.0/24",command="/home/u/.local/bin/hal-remote serve --client laptop '
    '--hub /home/u/.local/bin/hal-hub" ssh-ed25519 ' + BLOB + " hal-remote:laptop:mcp"
)


def _render(role, **over):
    args = dict(
        client="laptop", key=KEY, from_pattern="192.168.1.0/24", forwards=FORWARDS,
        serve_path=SERVE, hub_path=HUB,
    )  # fmt: skip
    args.update(over)
    return authkeys.render_line(role, **args)


def test_tunnel_line_matches_the_golden():
    assert _render("tunnel") == TUNNEL_GOLDEN


def test_mcp_line_matches_the_golden_and_has_no_port_forwarding():
    line = _render("mcp")
    assert line == MCP_GOLDEN
    assert "port-forwarding" not in line


def test_from_any_omits_the_option():
    assert "from=" not in _render("tunnel", from_pattern=None)


def test_expiry_is_added_to_both_roles():
    for role in ("tunnel", "mcp"):
        assert 'expiry-time="20261231Z"' in _render(role, expires=date(2026, 12, 31))


def test_each_forward_gets_its_own_permitopen():
    line = _render("tunnel", forwards=(Forward("gateway", 1, 8741), Forward("gateway", 2, 9000)))
    assert 'permitopen="127.0.0.1:8741",permitopen="127.0.0.1:9000"' in line


def test_path_with_a_space_is_single_quoted():
    line = _render("mcp", serve_path=Path("/home/a b/hal-remote"))
    assert "command=\"'/home/a b/hal-remote' serve" in line


@pytest.mark.parametrize("bad", ['/x/a"b', "/x/a\\b", "/x/a\nb", "/x/a\0b", "relative/hal"])
def test_unsafe_or_relative_path_is_refused(bad):
    with pytest.raises(authkeys.AuthKeysError):
        _render("mcp", serve_path=Path(bad))


@pytest.mark.parametrize("bad", ['1.2.3.4",command="x', "a b", ""])
def test_hostile_from_pattern_is_refused(bad):
    with pytest.raises(authkeys.AuthKeysError):
        _render("tunnel", from_pattern=bad)


def test_bad_client_name_is_refused():
    with pytest.raises(ValueError):
        _render("mcp", client='x" y')


def test_tunnel_line_without_forwards_is_refused():
    with pytest.raises(authkeys.AuthKeysError):
        _render("tunnel", forwards=())


FOREIGN = (
    "# my keys\r\n"
    "\r\n"
    'command="backup" ssh-rsa AAAAB3Nza me@box\r\n'
    "ssh-ed25519 AAAAC3Nza other@host"  # no final newline
)


def test_merge_keeps_foreign_lines_byte_for_byte_and_appends():
    merged = authkeys.merge(FOREIGN, "laptop", [TUNNEL_GOLDEN, MCP_GOLDEN])
    assert merged.startswith(FOREIGN + "\n")
    assert merged.endswith(MCP_GOLDEN + "\n")


def test_merge_replaces_the_lines_of_the_same_client_only():
    other = TUNNEL_GOLDEN.replace(":laptop:", ":phone:")
    first = authkeys.merge(FOREIGN, "laptop", [TUNNEL_GOLDEN, MCP_GOLDEN])
    first = authkeys.merge(first, "phone", [other])
    again = authkeys.merge(first, "laptop", [MCP_GOLDEN])
    assert again.count("hal-remote:laptop:") == 1
    assert other in again
    assert again.startswith(FOREIGN)


def test_merge_refuses_a_line_for_another_client():
    with pytest.raises(authkeys.AuthKeysError):
        authkeys.merge("", "phone", [TUNNEL_GOLDEN])


def test_remove_drops_only_the_named_client_and_counts():
    text = authkeys.merge(FOREIGN, "laptop", [TUNNEL_GOLDEN, MCP_GOLDEN])
    out, dropped = authkeys.remove(text, "laptop")
    assert dropped == 2
    assert out == FOREIGN + "\n"
    assert authkeys.remove(out, "laptop") == (out, 0)


def test_a_foreign_comment_that_mentions_the_tag_is_not_removed():
    line = "ssh-ed25519 " + BLOB + " note about hal-remote:laptop:tunnel\n"
    assert authkeys.remove(line, "laptop") == (line, 0)


def test_parse_round_trips_options_and_key():
    text = FOREIGN + "\n" + TUNNEL_GOLDEN + "\n" + MCP_GOLDEN + "\n"
    owned, other = authkeys.parse(text)
    assert other == 3
    assert [(o.client, o.role) for o in owned] == [("laptop", "tunnel"), ("laptop", "mcp")]
    assert owned[0].options[:2] == ("restrict", "port-forwarding")
    assert owned[1].options[-1].startswith("command=")
    assert owned[0].key.blob == BLOB


def test_write_sets_modes_and_content(tmp_path):
    path = tmp_path / ".ssh" / "authorized_keys"
    authkeys.write(path, "x\n")
    assert path.read_text() == "x\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_update_replaces_a_clients_lines_in_place(tmp_path):
    path = tmp_path / ".ssh" / "authorized_keys"
    authkeys.write(path, FOREIGN)
    authkeys.update(path, "laptop", [TUNNEL_GOLDEN])
    authkeys.update(path, "laptop", [MCP_GOLDEN])
    text = path.read_bytes().decode()
    assert text.startswith(FOREIGN) and text.count("hal-remote:laptop:") == 1


@pytest.mark.skipif(authkeys.fcntl is None, reason="needs fcntl locks")
def test_held_lock_times_out_with_a_clear_error(tmp_path):
    path = tmp_path / ".ssh" / "authorized_keys"
    path.parent.mkdir()
    started, release = threading.Event(), threading.Event()

    def hold():
        with authkeys._lock(path, 1.0):
            started.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    started.wait(5)
    try:
        # flock is per open file description, so a second descriptor in this process blocks
        with pytest.raises(authkeys.LockTimeout):
            authkeys.write(path, "y\n", lock_timeout_s=0.2)
    finally:
        release.set()
        t.join()
    assert not os.path.exists(path)

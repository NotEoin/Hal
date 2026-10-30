"""Tests for the `hal-remote` CLI skeleton."""

import pytest

from halkit import config
from halremote import cli


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_unimplemented_command_exits_2_and_says_so(command, capsys):
    assert cli.main([command]) == cli.EXIT_USAGE
    assert "not implemented" in capsys.readouterr().err


def test_missing_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == cli.EXIT_USAGE


def test_remote_tables_are_registered_with_the_documented_defaults():
    tables = {t.name: t for t in config.known_tables()}
    assert {k.name: k.default for k in tables["remote"].keys}["desktop_tools"] is True
    server = {k.name: k.default for k in tables["remote.server"].keys}
    assert server["max_sessions"] == 4
    assert server["delivery_ttl_s"] == 86400
    assert (server["from_prefix_v4"], server["from_prefix_v6"]) == (24, 64)


@pytest.mark.parametrize("key", ["from_prefix_v4", "from_prefix_v6"])
def test_prefix_check_rejects_zero(key):
    spec = next(k for k in config.known_tables() if k.name == "remote.server").keys
    check = next(k for k in spec if k.name == key).check
    assert check(0) is not None
    assert check(24) is None


def test_package_carries_the_workspace_version():
    import halremote

    assert halremote.__version__ == "0.1.0.dev0"

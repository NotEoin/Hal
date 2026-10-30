"""Tests for the `hal` command table."""

import pytest

from halcore import cli


def test_bare_hal_resolves_to_chat():
    command, args = cli.resolve([])
    assert command.module == "chat" and args == []


def test_print_flag_runs_headless_without_the_flag_in_its_arguments():
    command, args = cli.resolve(["-p", "fix it"])
    assert command.module == "headless" and args == ["fix it"]


@pytest.mark.parametrize("name", ["lockdown", "paranoia", "policy", "audit", "approve", "deny", "approvals"])
def test_shared_modules_get_their_command_name_first(name):
    command, args = cli.resolve([name, "x"])
    assert args == [name, "x"]


def test_power_shortcuts_run_the_power_module():
    command, args = cli.resolve(["off"])
    assert command.module == "power" and args == ["off"]


def test_install_alias_prints_a_notice_and_runs_units_install(capsys):
    command, args = cli.resolve(["install"])
    assert command.module == "units" and args == ["install", "--start"]
    assert "now 'hal units install --start'" in capsys.readouterr().err


def test_unknown_command_exits_with_the_usage_code(capsys):
    assert cli.main(["nonsense"]) == cli.EXIT_USAGE
    assert "unknown command" in capsys.readouterr().err


def test_help_lists_every_command(capsys):
    assert cli.main(["--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert all(name in out for name in cli.COMMANDS)


def test_every_command_module_exposes_main():
    for command in cli.COMMANDS.values():
        assert callable(cli.load(command))
    assert callable(cli.load(cli.HEADLESS))

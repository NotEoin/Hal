"""Tests for the vault commands handing off to the memory pack's `hal-memory`."""

from __future__ import annotations

from halcore import cli, memorycmd


def test_every_vault_command_resolves_to_the_memory_hand_off():
    # regression: the removed bash launcher sent these to hal-memory; the hal command did not know them
    for name in memorycmd.VAULT_COMMANDS:
        command, rest = cli.resolve([name, "x"])
        assert (command.module, rest) == ("memorycmd", [name, "x"])


def test_a_missing_memory_pack_says_how_to_add_it(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(memorycmd.shutil, "which", lambda name: None)
    monkeypatch.setenv("HAL_DATA_DIR", str(tmp_path))
    assert memorycmd.main(["ask", "what is due"]) == memorycmd.EXIT_MISSING
    assert "hal packs add memory" in capsys.readouterr().err


def test_the_pack_program_in_the_shared_environment_takes_over(monkeypatch, tmp_path):
    program = tmp_path / "envs" / "packs" / "bin" / "hal-memory"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    monkeypatch.setattr(memorycmd.shutil, "which", lambda name: None)
    monkeypatch.setenv("HAL_DATA_DIR", str(tmp_path))
    ran = []
    monkeypatch.setattr(memorycmd.os, "execv", lambda path, argv: ran.append((path, argv)))
    memorycmd.main(["index", "--full"])
    assert ran == [(program, [str(program), "index", "--full"])]

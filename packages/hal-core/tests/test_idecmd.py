"""Tests for `hal ide setup`: print by default, write only the named file, never overwrite silently."""

import json

from halcore import idecmd


def test_print_is_the_default_and_zed_snippet_is_valid_json(capsys):
    assert idecmd.main(["setup", "zed"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["agent_servers"]["Hal"] == {"command": "hal", "args": ["acp"]}


def test_every_editor_has_an_ascii_snippet_and_hint():
    assert set(idecmd.SNIPPETS) == {"zed", "jetbrains", "neovim", "emacs", "vscode"}
    for name, text in idecmd.SNIPPETS.items():
        assert text.isascii() and idecmd.HINTS[name].isascii()


def test_write_creates_only_the_named_file(tmp_path):
    target = tmp_path / "sub" / "acp.json"
    assert idecmd.main(["setup", "jetbrains", "--write", str(target)]) == 0
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["acp.json"]


def test_write_refuses_an_existing_file_without_force(tmp_path, capsys):
    target = tmp_path / "settings.json"
    target.write_text("mine")
    assert idecmd.main(["setup", "zed", "--write", str(target)]) == idecmd.EXIT_REFUSED
    assert target.read_text() == "mine" and "--force" in capsys.readouterr().err


def test_force_keeps_a_backup_of_the_old_file(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("mine")
    assert idecmd.main(["setup", "zed", "--write", str(target), "--force"]) == 0
    assert (tmp_path / "settings.json.bak").read_text() == "mine"
    assert "agent_servers" in target.read_text()


def test_write_refuses_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert idecmd.main(["setup", "zed", "--write", str(link), "--force"]) == idecmd.EXIT_REFUSED
    assert real.read_text() == "x"

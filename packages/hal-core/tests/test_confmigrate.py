"""Tests for `hal configure --migrate` (REQ-CLI-84); the hal.conf fixtures are synthetic."""

from __future__ import annotations

import tomllib

from halcore import configure, confmigrate

CONF = """# comment
HAL_PROFILE="24gb"
HAL_MODEL="gemma-a"
HAL_MAX_OUTPUT_TOKENS=8192
HAL_ALLOW_NETWORK="1"
HAL_NUM_CTX="16384"
HAL_UNIVERSITY='Example U'
echo $(rm -rf /)
"""


def test_migrate_maps_hal_conf_keys():
    new, notes = confmigrate.migrate_text("", confmigrate.parse_conf(CONF), "/x/hal.conf")
    data = tomllib.loads(new)
    assert data["hal"]["profile"] == "24gb"
    assert data["compat"]["hal_model"] == "gemma-a"
    assert data["claude"]["max_output_tokens"] == 8192
    assert data["claude"]["allow_network"] is True
    assert data["claude"]["env"]["HAL_UNIVERSITY"] == "Example U"
    assert data["compat"]["hal_conf"] == ["/x/hal.conf"]
    assert any("HAL_NUM_CTX dropped" in n for n in notes)


def test_migrate_writes_explicit_jobhunt_root():
    new, _ = confmigrate.migrate_text("", {}, "/x/hal.conf")
    assert tomllib.loads(new)["jobs"]["jobhunt_root"] == confmigrate.JOBHUNT_ROOT_DEFAULT


def test_migrate_keeps_a_jobs_table_that_already_sets_the_root():
    old = '[jobs]\njobhunt_root = "/mine"\n'
    new, _ = confmigrate.migrate_text(old, {}, "/x/hal.conf")
    assert tomllib.loads(new)["jobs"]["jobhunt_root"] == "/mine"


def test_migrate_never_overwrites_an_existing_key():
    old = '[hal]\nprofile = "max"\n'
    new, notes = confmigrate.migrate_text(old, {"HAL_PROFILE": "12gb"}, "/x/hal.conf")
    assert tomllib.loads(new)["hal"]["profile"] == "max"
    assert any("already set" in n for n in notes)


def test_migrate_command_writes_after_yes_and_keeps_a_backup(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config.toml"
    target.write_text('[hal]\nchat_model = "mine"\n')
    conf = tmp_path / "hal.conf"
    conf.write_text('HAL_MODEL="m1"\n')
    monkeypatch.setenv("HAL_CONFIG", str(target))
    assert configure.main(["--migrate", str(conf), "--yes"]) == 0
    assert tomllib.loads(target.read_text())["compat"]["hal_model"] == "m1"
    assert list(tmp_path.glob("config.toml.bak.*"))
    assert "+hal_model" in capsys.readouterr().out.replace(" = ", "")


def test_migrate_without_terminal_or_yes_writes_nothing(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    conf = tmp_path / "hal.conf"
    conf.write_text('HAL_MODEL="m1"\n')
    monkeypatch.setenv("HAL_CONFIG", str(target))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert configure.main(["--migrate", str(conf)]) == 2
    assert not target.exists()

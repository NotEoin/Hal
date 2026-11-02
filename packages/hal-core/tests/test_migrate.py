"""Tests for `halcore.migrate` (`hal migrate ecosystem` and `undo`), with a synthetic layout."""

import os

import pytest

from halcore import cli, migrate
from halkit.testing import TempHalHome


@pytest.fixture
def world(tmp_path, monkeypatch):
    with TempHalHome() as h:
        home = tmp_path / "home"
        (home / ".local/bin").mkdir(parents=True)
        eco = tmp_path / "hal-ecosystem"
        (eco / "core/hal").mkdir(parents=True)
        (eco / "core/hal/hal.conf").write_text('HAL_PROFILE="24gb"\n')
        venv = h.data / "venv/bin"
        venv.mkdir(parents=True)
        for name in migrate.LINK_NAMES:
            (venv / name).write_text("#!/bin/sh\n")
        (home / ".local/bin/hal").symlink_to(eco / "core/hal/hal")
        h.config.write_text('[ui]\ndefault_mode = "auto"\n')
        yield h, home, eco


def snapshot(*roots):
    out = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            out[str(p)] = os.readlink(p) if p.is_symlink() else (p.read_bytes() if p.is_file() else None)
    return out


def test_dry_run_changes_nothing_and_lists_every_change(world, capsys):
    h, home, eco = world
    before = snapshot(h.root, home, eco)
    plan = migrate.build_plan(eco, home=home)
    kinds = [s.kind for s in plan.steps]
    assert "config" in kinds and kinds.count("link") == 4
    assert snapshot(h.root, home, eco) == before


def test_migrate_then_undo_restores_tree_byte_for_byte(world, monkeypatch):
    h, home, eco = world
    monkeypatch.setenv("HOME", str(home))
    before = snapshot(home, eco, h.config.parent)
    journal = migrate.apply(migrate.build_plan(eco, home=home))
    text = h.config.read_text()
    assert "jobhunt_root" in text and str(eco / "core/hal/hal.conf") in text
    assert os.readlink(home / ".local/bin/hal") == str(h.data / "venv/bin/hal")
    migrate.undo(journal)
    assert snapshot(home, eco, h.config.parent) == before


def test_undo_restores_a_symlinked_config_as_the_same_link(world, tmp_path, monkeypatch):
    # Regression: apply replaced the config link with a file and undo left the file there.
    h, home, eco = world
    monkeypatch.setenv("HOME", str(home))
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "config.toml").write_text(h.config.read_text())
    h.config.unlink()
    h.config.symlink_to(dotfiles / "config.toml")
    before = snapshot(home, eco, h.config.parent, dotfiles)
    journal = migrate.apply(migrate.build_plan(eco, home=home))
    assert "jobhunt_root" in h.config.read_text()
    migrate.undo(journal)
    assert snapshot(home, eco, h.config.parent, dotfiles) == before


def test_migrate_writes_nothing_under_from_vault_or_employment(world, tmp_path):
    h, home, eco = world
    with pytest.raises(migrate.MigrateError):
        migrate.guard_target(eco / "core/x", eco)
    with pytest.raises(migrate.MigrateError):
        migrate.guard_target(tmp_path / "employment-helper-2/x", eco)
    before = snapshot(eco)
    migrate.apply(migrate.build_plan(eco, home=home))
    assert snapshot(eco) == before


def test_existing_compat_table_is_reported_manual_not_duplicated(world):
    h, home, eco = world
    h.config.write_text('[compat]\nhal_conf = []\n[jobs]\njobhunt_root = "x"\n')
    plan = migrate.build_plan(eco, home=home)
    assert not any(s.kind == "config" for s in plan.steps)
    assert any("[compat].hal_conf" in s.detail for s in plan.steps)


def test_cli_defaults_to_dry_run(world, capsys):
    h, home, eco = world
    assert migrate.main(["ecosystem", "--from", str(eco)]) == 0
    assert "dry run" in capsys.readouterr().out
    assert not (h.state / "migrate").exists()


def test_missing_source_fails_loudly(world, tmp_path, capsys):
    assert migrate.main(["ecosystem", "--from", str(tmp_path / "nope")]) == 1
    assert "not a folder" in capsys.readouterr().err


def test_migrate_is_a_command():
    assert cli.resolve(["migrate", "undo", "j"])[0].module == "migrate"


def test_every_old_unit_is_mapped_to_its_new_source(world):
    _, home, eco = world
    notes = "\n".join(s.detail for s in migrate.build_plan(eco, home=home).steps if s.kind == "manual")
    wanted = ["hal-briefing@", "hal-calendar-reminders", "hal-clock", "hal-rgb-server", "hal-routines", "hal-tasks-reminders"]
    for name in wanted:
        assert f"unit {name}" in notes
    assert "hal-brief.service: retired" in notes


@pytest.mark.parametrize(
    "body",
    [
        '[voice]\nmodule_paths = ["/v"]\n\n[hub]\nmodule_paths = ["/x/hal-ecosystem/core"]\n',
        '[hub]\nmodule_paths = ["/x/hal-ecosystem/a]b", "/mine"]\n',
    ],
    ids=["another-table-first", "bracket-in-a-path"],
)
def test_module_paths_rewrite_that_would_break_the_config_is_left_manual(tmp_path, body):
    # Regression: the line regex rewrote another table's key, or cut a path at a `]`.
    cfg = tmp_path / "config.toml"
    cfg.write_text(body)
    step = migrate.module_paths_step(tmp_path / "hal-ecosystem", cfg)
    assert step is not None and step.kind == "manual" and "module_paths" in step.detail

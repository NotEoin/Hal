"""Tests for `hal setup`: the plan, `--dry-run`, idempotence and the unit rules.

Every external command is replaced; nothing here runs uv, docker or systemctl.
Run with: uv run pytest packages/hal-core
"""

from pathlib import Path

import pytest

from halcore import setup as hsetup
from halcore import units


def make_repo(base: Path, name: str, module: bool = False) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0"\n')
    if module:
        (repo / "mod").mkdir()
        (repo / "mod" / "hal-module.toml").write_text('name = "x"\n')
    return repo


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Sibling repos, an empty pack record and no gpu-light, docker or images."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xstate"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdata"))
    monkeypatch.setenv("HAL_GPU_LIGHT", "off")
    monkeypatch.setattr(hsetup.shutil, "which", lambda name: None)
    monkeypatch.setattr(hsetup.packs, "read_record", lambda: {"packs": {}})
    added: list[Path] = []
    monkeypatch.setattr(hsetup.packs, "add_local", lambda p: added.append(p) or p.name)
    sibs = tmp_path / "sibs"
    make_repo(sibs, "hal-memory")
    make_repo(sibs, "hal-voice")
    return sibs, added


def test_plan_installs_the_packs_found_as_siblings_and_skips_the_rest(world):
    sibs, _ = world
    steps = {s.name: s for s in hsetup.plan([sibs], enable=False, image=False)}
    assert steps["pack memory"].run is not None and steps["pack voice"].run is not None
    assert "not found" in steps["pack hal-tools"].skip
    assert "Works without" not in steps["gpu-light"].skip and steps["gpu-light"].skip


def test_hal_tools_packs_are_installed_one_by_one(world):
    sibs, _ = world
    tools = make_repo(sibs, "hal-tools")
    make_repo(tools / "packs", "hal-organiser")
    make_repo(tools / "packs", "hal-life")
    names = [s.name for s in hsetup.plan([sibs], enable=False, image=False)]
    assert "pack organiser" in names and "pack life" in names


def test_dry_run_prints_every_step_and_runs_none(world, capsys):
    sibs, added = world
    lines: list[str] = []
    ok = hsetup.execute(hsetup.plan([sibs], enable=False, image=False), dry_run=True, out=lines.append)
    assert ok and added == []
    assert any(line.startswith("[plan] pack memory") for line in lines)


def test_installed_packs_are_skipped_on_a_second_run(world, monkeypatch):
    sibs, _ = world
    monkeypatch.setattr(hsetup.packs, "read_record", lambda: {"packs": {"memory": {}, "voice": {}}})
    steps = {s.name: s for s in hsetup.plan([sibs], enable=False, image=False)}
    assert steps["pack memory"].skip == "already installed"


def test_employment_helper_without_a_hal_module_is_left_to_run_on_its_own(world):
    sibs, _ = world
    make_repo(sibs, "employment-helper")
    step = next(s for s in hsetup.plan([sibs], enable=False, image=False) if s.name == "employment helper")
    assert step.run is None and "on its own" in step.skip


def test_employment_helper_with_a_hal_module_is_installed_as_a_pack(world):
    sibs, added = world
    repo = make_repo(sibs, "employment-helper", module=True)
    step = next(s for s in hsetup.plan([sibs], enable=False, image=False) if s.name == "employment helper")
    step.run()
    assert added == [repo]


def test_a_failing_step_is_reported_and_the_rest_still_run(world):
    def boom() -> str:
        raise hsetup.packs.PackError("no network")

    lines: list[str] = []
    steps = [hsetup.Step("a", "x", boom), hsetup.Step("b", "y", lambda: "fine")]
    assert hsetup.execute(steps, dry_run=False, out=lines.append) is False
    assert lines[0].startswith("[FAIL]") and lines[1].startswith("[ ok ]")


def test_units_are_written_but_not_enabled_without_the_enable_option(tmp_path, monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(units, "UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(units, "module_roots", lambda: [])
    monkeypatch.setattr(units, "systemctl", lambda *a, **k: calls.append(a))
    units.install(desktop=False, enable=False)
    assert ("daemon-reload",) in calls and not any(c[0] in ("enable", "start") for c in calls)


def test_setup_is_a_listed_command():
    from halcore import cli

    assert "setup" in cli.COMMANDS and cli.resolve(["setup", "--dry-run"])[0].module == "setup"


@pytest.fixture
def old_install(tmp_path, monkeypatch):
    """A checkout with one unit, and a unit folder that holds an older install's units."""
    root = tmp_path / "next"
    (root / "systemd").mkdir(parents=True)
    (root / "systemd" / "hal-console.service").write_text("[Unit]\n")
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "hal-console.service").write_text("[Unit]\n# old\n")
    (unit_dir / "hal-clock.service").write_text("[Unit]\n# old\n")
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(units, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(units, "module_roots", lambda: [root])
    monkeypatch.setattr(hsetup.shutil, "which", lambda name: "/usr/bin/" + name if name == "systemctl" else None)
    units._save({"hal-clock.service": str(tmp_path / "hal-ecosystem" / "hal-clock")})
    return unit_dir


def test_setup_refuses_to_replace_another_installs_units_even_in_a_dry_run(old_install):
    # Regression: `hal setup` overwrote, stopped and deleted the live hal-ecosystem units,
    # because both installs share the unit record, while its dry run said only "write".
    step = hsetup._units_step(enable=False, replace=False)
    lines: list[str] = []
    assert hsetup.execute([step], dry_run=True, out=lines.append) is False
    assert lines[0].startswith("[FAIL]") and "hal-clock.service" in lines[0] and "hal-console.service" in lines[0]
    assert step.run is None and "# old" in (old_install / "hal-console.service").read_text()


def test_setup_with_replace_names_the_units_it_will_replace(old_install):
    step = hsetup._units_step(enable=False, replace=True)
    assert step.run is not None and not step.blocked
    assert "replacing" in step.detail and "hal-clock.service" in step.detail


def test_units_this_checkout_wrote_are_not_foreign(old_install, tmp_path):
    units._save({"hal-console.service": str(tmp_path / "next")})
    assert units.foreign_units() == []

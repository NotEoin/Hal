"""Tests for `halcore.units`: unit rendering, the GPU guard and the gpu-light login unit.

systemctl is replaced in-process; nothing here starts a service.
Run with: uv run pytest packages/hal-core
"""
import os
import unittest

import pytest
from pathlib import Path
from unittest import mock

from halcore_env import ROOT, TMP, LightCase
from halcore import power, units


class UnitsTest(unittest.TestCase):
    def test_render_and_gpu_guard(self):
        root = Path(TMP) / "mod"
        (root / "systemd").mkdir(parents=True)
        (root / "systemd" / "x.service").write_text(
            "[Unit]\nDescription=x\nX-HalGPU=yes\n\n[Service]\nWorkingDirectory=@ROOT@\nExecStart=@CORE@/hal\n")
        units.UNIT_DIR = Path(TMP) / "units"
        record = {}
        self.assertEqual(units.install_module(root, record), ["x.service"])
        text = (units.UNIT_DIR / "x.service").read_text()
        self.assertIn(f"WorkingDirectory={root}", text)
        self.assertIn("ConditionPathExists=!", text)
        self.assertTrue(units.is_gpu(units.UNIT_DIR / "x.service"))


class OffUnitTest(LightCase):
    """Renders units the way `hal units install` does, into a temporary unit folder."""

    def setUp(self):
        super().setUp()
        self.root = self.dir / "core"
        (self.root / "systemd").mkdir(parents=True)
        for patch in (mock.patch.object(units, "UNIT_DIR", self.dir / "units"),
                      mock.patch.object(units, "module_roots", lambda: [self.root]),
                      mock.patch.object(units, "installed", lambda: self.unit_record),
                      self.fake_systemctl()):
            patch.start()
            self.addCleanup(patch.stop)
        self.unit_record: dict[str, str] = {}

    def add_service(self, name: str, mark: str = "") -> None:
        unit = f"[Unit]\nDescription={name}\n{mark}\n[Service]\nExecStart=/bin/true\n"
        (self.root / "systemd" / name).write_text(unit)

    def test_x_hal_off_unit_gets_the_off_condition_without_being_a_gpu_unit(self):
        self.add_service("voice.service", "X-HalOff=yes\n")
        units.install_module(self.root, self.unit_record)
        dest = units.UNIT_DIR / "voice.service"
        self.assertIn("ConditionPathExists=!", dest.read_text())
        self.assertTrue(units.follows_off(dest))
        self.assertFalse(units.is_gpu(dest))

    def test_plain_unit_gets_no_off_condition(self):
        self.add_service("reminders.service")
        units.install_module(self.root, self.unit_record)
        dest = units.UNIT_DIR / "reminders.service"
        self.assertNotIn("ConditionPathExists", dest.read_text())
        self.assertFalse(units.follows_off(dest))

    def test_hal_off_stops_x_hal_off_and_gpu_units_but_not_plain_ones(self):
        self.add_service("voice.service", "X-HalOff=yes\n")
        self.add_service("memory.service", "X-HalGPU=yes\n")
        self.add_service("reminders.service")
        units.install_module(self.root, self.unit_record)
        with self.fake_ollama([]), mock.patch.object(power, "_notify", lambda *a: None):
            power.off(quiet=True)
        self.assertIn("systemctl stop memory.service voice.service", self.calls())

    def test_status_counts_the_units_that_follow_hal_off(self):
        self.add_service("voice.service", "X-HalOff=yes\n")
        self.add_service("reminders.service")
        units.install_module(self.root, self.unit_record)
        with mock.patch.object(power, "loaded_models", list):
            self.assertIn("0 of 1 services running", power.status())

    def test_install_removes_a_left_over_light_unit(self):
        # Regression: the old boot-claim unit kept a standing `hal` claim after the upgrade.
        units.UNIT_DIR.mkdir(parents=True)
        old = units.UNIT_DIR / units.LEGACY_LIGHT_UNIT
        old.write_text("[Unit]\nDescription=old\n")
        self.unit_record[units.LEGACY_LIGHT_UNIT] = str(self.root)
        with mock.patch.object(units, "_save", lambda record: None), \
                mock.patch.object(units, "install_desktop", lambda root: []):
            units.install(enable=False)
        self.assertFalse(old.exists())
        self.assertNotIn(units.LEGACY_LIGHT_UNIT, self.unit_record)
        self.assertIn(f"systemctl disable --now {units.LEGACY_LIGHT_UNIT}", self.calls())

    def test_core_hal_no_longer_ships_the_light_unit(self):
        self.assertFalse((ROOT / "systemd" / units.LEGACY_LIGHT_UNIT).exists())


if __name__ == "__main__":
    unittest.main()


class JobsUnitTest(unittest.TestCase):
    def test_jobs_timer_starts_a_service_that_waits_for_the_workers(self):
        service = units.render((ROOT / "systemd" / "hal-jobs.service").read_text(), ROOT)
        timer = (ROOT / "systemd" / "hal-jobs.timer").read_text()
        self.assertIn("-m halcore jobs run --wait", service)
        self.assertNotIn("@", service)
        self.assertNotIn("X-HalGPU", service)  # jobs ride gpu-light as class batch, so hal off must not stop them
        self.assertIn("Unit=hal-jobs.service", timer)
        self.assertIn("WantedBy=timers.target", timer)


def _register_pack(state, name, checkout, python, modules):
    """Writes a pack record whose modules live under `checkout/<module>/hal-module.toml`."""
    import json

    mods = {}
    for module in modules:
        folder = checkout / module
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "hal-module.toml").write_text(f'[module]\nname = "{module}"\n')
        mods[module] = {"manifest": str(folder / "hal-module.toml"), "interpreter": [python]}
    state.mkdir(parents=True, exist_ok=True)
    record = {"version": 2, "packs": {name: {"python": python, "path": str(checkout), "modules": mods}}}
    (state / "packs.json").write_text(json.dumps(record))


def test_pack_modules_win_over_old_module_paths_and_use_the_pack_python(tmp_path, monkeypatch):
    # Regression: units rendered into the old hal-ecosystem tree and /usr/bin/python3 after cutover.
    state, old, new = tmp_path / "state", tmp_path / "old", tmp_path / "new"
    for folder in (old / "organiser", old / "legacy"):
        folder.mkdir(parents=True)
        name = folder.name
        (folder / "hal-module.toml").write_text(f'[module]\nname = "{name}"\n')
    _register_pack(state, "organiser", new, "/envs/organiser/bin/python", ["organiser"])
    (new / "systemd").mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[hub]\nmodule_paths = ["{old}"]\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    monkeypatch.setenv("HAL_STATE", str(state))
    roots = units.module_roots()
    assert new / "organiser" in roots and old / "organiser" not in roots
    assert new in roots and old / "legacy" in roots
    assert units.python_for(new / "organiser") == "/envs/organiser/bin/python"
    assert units.python_for(old / "legacy") == units.DEFAULT_PYTHON
    assert units.render("@PYTHON@ -m x", new / "organiser") == "/envs/organiser/bin/python -m x"


def test_disabled_pack_modules_get_no_units(tmp_path, monkeypatch):
    state, new = tmp_path / "state", tmp_path / "new"
    _register_pack(state, "life", new, "/p/python", ["habits"])
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hub]\nmodule_paths = []\ndisabled = ["habits"]\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    monkeypatch.setenv("HAL_STATE", str(state))
    assert new / "habits" not in units.module_roots()


SIBLING_REPOS = ("hal-tools", "hal-memory", "hal-voice")
SIBLING_PARENT = ROOT.parent  # the checkout's sibling repos sit next to it, as `hal setup` expects


def _register_checkout(state, repo, python):
    """Registers each pack folder of a sibling the way `hal packs add --local` records it."""
    import json

    from halcore import packs, setup

    record = {"version": 2, "packs": {}}
    for path in setup.pack_dirs(repo):
        record["packs"][path.name] = {"python": python, "path": str(path), "modules": packs.scan_modules(path, Path(python))}
    state.mkdir(parents=True, exist_ok=True)
    existing = json.loads((state / "packs.json").read_text()) if (state / "packs.json").exists() else {"version": 2, "packs": {}}
    existing["packs"].update(record["packs"])
    (state / "packs.json").write_text(json.dumps(existing))


def test_missing_exec_start_programs_are_reported(tmp_path, monkeypatch):
    state, new = tmp_path / "state", tmp_path / "new"
    _register_pack(state, "life", new, "/p/python", ["habits"])
    (new / "habits" / "systemd").mkdir()
    (new / "habits" / "systemd" / "a.service").write_text("[Service]\nExecStart=@ROOT@/bin/serve\n")
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("HAL_STATE", str(state))
    assert units.missing_exec_paths() == [f"a.service: {new}/habits/bin/serve"]
    (new / "habits" / "bin").mkdir()
    (new / "habits" / "bin" / "serve").write_text("#!/bin/sh\n")
    assert units.missing_exec_paths() == []


def test_units_outside_the_registered_packs_are_listed(tmp_path, monkeypatch):
    old = tmp_path / "old" / "clock"
    (old / "systemd").mkdir(parents=True)
    (old / "hal-module.toml").write_text('[module]\nname = "clock"\n')
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[hub]\nmodule_paths = ["{old.parent}"]\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    assert units.outside_packs() == [str(old)]


def test_every_sibling_pack_unit_runs_a_program_that_exists(tmp_path, monkeypatch):
    repos = [SIBLING_PARENT / name for name in SIBLING_REPOS if (SIBLING_PARENT / name).is_dir()]
    if not repos:
        pytest.skip("no sibling pack checkouts next to this one")
    import sys

    for repo in repos:
        _register_checkout(tmp_path / "state", repo, sys.executable)
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    assert units.exec_starts()
    assert units.missing_exec_paths() == []


def test_empty_module_paths_render_no_units_from_the_old_ecosystem_folders(tmp_path, monkeypatch):
    # Regression: an empty `[hub].module_paths` fell back to ecosystem/core and the extensions folder.
    legacy = tmp_path / "extensions" / "clock"
    (legacy / "systemd").mkdir(parents=True)
    (legacy / "hal-module.toml").write_text('[module]\nname = "clock"\n')
    monkeypatch.setattr(units.config, "ECOSYSTEM", tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[hal]\nextensions_dir = "{legacy.parent}"\n\n[hub]\nmodule_paths = []\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    assert units.module_roots() == [units.config.ROOT]


def test_core_units_run_on_core_hal_environment(tmp_path, monkeypatch):
    # regression: core units rendered with /usr/bin/python3, which cannot import halcore
    monkeypatch.setenv("HAL_STATE", str(tmp_path / "state"))
    python = units.python_for(units.config.ROOT)
    assert python != units.DEFAULT_PYTHON
    for name in ("hal-console.service", "hal-jobs.service"):
        text = units.render((units.config.ROOT / "systemd" / name).read_text(), units.config.ROOT)
        assert f"ExecStart={python} -m halcore" in text

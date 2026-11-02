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


class LightUnitTest(LightCase):
    """Renders the login unit the way `hal units install` does, into a temporary unit folder."""

    def setUp(self):
        super().setUp()
        self.root = self.dir / "core"
        (self.root / "systemd").mkdir(parents=True)
        (self.root / "systemd" / units.LIGHT_UNIT).write_text((ROOT / "systemd" / units.LIGHT_UNIT).read_text())
        for patch in (mock.patch.object(units, "UNIT_DIR", self.dir / "units"),
                      mock.patch.object(units, "module_roots", lambda: [self.root]), self.fake_systemctl()):
            patch.start()
            self.addCleanup(patch.stop)

    def add_service(self, name: str, gpu: bool) -> None:
        mark = "X-HalGPU=yes\n" if gpu else ""
        unit = f"[Unit]\nDescription={name}\n{mark}\n[Service]\nExecStart=/bin/true\n"
        (self.root / "systemd" / name).write_text(unit)

    def test_light_unit_is_ordered_before_every_gpu_service(self):
        self.add_service("voice.service", gpu=True)
        self.add_service("tts.service", gpu=True)
        self.add_service("reminders.service", gpu=False)
        record = {}
        self.assertIn(units.LIGHT_UNIT, units.install_module(self.root, record))
        text = (units.UNIT_DIR / units.LIGHT_UNIT).read_text()
        self.assertIn("\nBefore=tts.service voice.service\n", text)
        claim = 'claim --name hal --reason "hal.target" --wait --timeout 90'
        self.assertIn(f"ExecStart={self.dir}/gpu-light {claim}", text)
        self.assertIn(f"ExecStop={self.dir}/gpu-light release --name hal", text)
        # While Hal is off, a login must not claim the GPU either
        self.assertIn("ConditionPathExists=!", text)
        self.assertNotIn("@", text.replace(".service", ""))

    def test_light_unit_without_gpu_services_has_no_before_line(self):
        units.install_module(self.root, {})
        self.assertNotIn("Before=", (units.UNIT_DIR / units.LIGHT_UNIT).read_text())

    def test_light_unit_is_skipped_and_removed_without_gpu_light(self):
        record, skipped = {}, []
        units.install_module(self.root, record)
        with mock.patch.dict(os.environ, {"HAL_GPU_LIGHT": "off"}):
            self.assertEqual(units.install_module(self.root, record, skipped), [])
        self.assertFalse((units.UNIT_DIR / units.LIGHT_UNIT).exists())
        self.assertNotIn(units.LIGHT_UNIT, record)
        self.assertIn(f"systemctl disable {units.LIGHT_UNIT}", self.calls())
        self.assertEqual(skipped, [f"{units.LIGHT_UNIT} (gpu-light is not installed)"])

    def test_status_does_not_count_the_light_unit_as_a_gpu_service(self):
        with mock.patch.object(units, "gpu_units", lambda: [units.LIGHT_UNIT, "voice.service"]), \
                mock.patch.object(power, "loaded_models", list):
            self.assertIn("0 of 1 GPU services running", power.status())


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

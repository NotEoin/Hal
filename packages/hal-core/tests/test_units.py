"""Tests for `halcore.units`: unit rendering, the GPU guard and the gpu-light login unit.

systemctl is replaced in-process; nothing here starts a service.
Run with: uv run pytest packages/hal-core
"""
import os
import unittest
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

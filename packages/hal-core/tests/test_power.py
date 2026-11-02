"""Tests for `halcore.power`: hal on makes no GPU claim, hal off releases an old one.

systemctl, Ollama and desktop notifications are replaced in-process; nothing here starts a
service, loads a model or touches a GPU.
Run with: uv run pytest packages/hal-core
"""
import json
import os
import unittest
from unittest import mock

from halcore_env import LightCase
from halcore import config, power


class PowerTest(LightCase):
    def setUp(self):
        super().setUp()
        for patch in (self.fake_systemctl(), self.fake_ollama([]),
                      mock.patch.object(power, "_notify", lambda *a: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_hal_on_makes_no_gpu_light_claim(self):
        # Regression: a standing `hal` claim kept gpu-light's batch job parked while Hal idled.
        msg = power.on(quiet=True)
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith("gpu-light")])
        self.assertIn("systemctl start hal.target", calls)
        self.assertNotIn("Warning", msg)

    def test_hal_on_removes_the_off_flag(self):
        (config.state_dir() / power.FLAG).touch()
        power.on(quiet=True)
        self.assertFalse(power.is_off())

    def test_hal_on_unloads_models_offloaded_to_ram(self):
        models = [{"name": "split", "size": 10, "size_vram": 6}, {"name": "whole", "size": 10, "size_vram": 10}]
        with self.fake_ollama(models):
            power.on(quiet=True)
        self.assertIn("ollama unload split", self.calls())
        self.assertNotIn("ollama unload whole", self.calls())

    def test_hal_off_releases_an_old_hal_claim_after_unloading_models(self):
        with self.fake_ollama([{"name": "chat", "size": 10, "size_vram": 10}]):
            power.off(quiet=True)
        calls = self.calls()
        self.assertLess(calls.index("ollama unload chat"), calls.index("gpu-light release --name hal"))

    def test_hal_on_and_off_work_without_gpu_light(self):
        with mock.patch.dict(os.environ, {"HAL_GPU_LIGHT": "off"}):
            on, off = power.on(quiet=True), power.off(quiet=True)
        self.assertEqual(self.calls(), ["systemctl start hal.target"])
        self.assertNotIn("Warning", on + off)

    def test_status_shows_the_light_and_its_reason(self):
        doc = self.dir / "status.json"
        doc.write_text(json.dumps({"effective": "red", "reason": "claim hal (boot)"}))
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_STATUS": str(doc)}):
            self.assertIn("GPU light: red (claim hal (boot))", power.status())


if __name__ == "__main__":
    unittest.main()

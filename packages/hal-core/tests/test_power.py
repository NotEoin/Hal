"""Tests for `halcore.power`: hal on, off and status claim and release the GPU light.

systemctl, Ollama and desktop notifications are replaced in-process; nothing here starts a
service, loads a model or touches a GPU.
Run with: uv run pytest packages/hal-core
"""
import json
import os
import unittest
from unittest import mock

from halcore_env import LightCase
from halcore import power


class PowerTest(LightCase):
    def setUp(self):
        super().setUp()
        for patch in (self.fake_systemctl(), self.fake_ollama([]),
                      mock.patch.object(power, "_notify", lambda *a: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_hal_on_claims_the_light_before_starting_hal_target(self):
        msg = power.on(quiet=True)
        calls = self.calls()
        claim = next(i for i, c in enumerate(calls) if c.startswith("gpu-light claim"))
        self.assertIn("--name hal --reason hal on --wait --timeout 90", calls[claim])
        self.assertLess(claim, calls.index("systemctl start hal.target"))
        self.assertNotIn("Warning", msg)

    def test_hal_on_starts_hal_anyway_when_the_wait_times_out(self):
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_CLAIM_EXIT": "4"}):
            msg = power.on(quiet=True)
        self.assertIn("systemctl start hal.target", self.calls())
        self.assertIn("the GPU is still busy after 90 s", msg)

    def test_hal_on_unloads_models_offloaded_to_ram_after_a_claim(self):
        models = [{"name": "split", "size": 10, "size_vram": 6}, {"name": "whole", "size": 10, "size_vram": 10}]
        with self.fake_ollama(models):
            power.on(quiet=True)
        self.assertIn("ollama unload split", self.calls())
        self.assertNotIn("ollama unload whole", self.calls())

    def test_hal_on_keeps_offloaded_models_when_the_gpu_is_still_busy(self):
        with self.fake_ollama([{"name": "split", "size": 10, "size_vram": 6}]), \
                mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_CLAIM_EXIT": "4"}):
            power.on(quiet=True)
        self.assertNotIn("ollama unload split", self.calls())

    def test_hal_off_releases_the_light_after_unloading_models(self):
        with self.fake_ollama([{"name": "chat", "size": 10, "size_vram": 10}]):
            power.off(quiet=True)
        calls = self.calls()
        self.assertLess(calls.index("ollama unload chat"), calls.index("gpu-light release --name hal"))

    def test_hal_on_and_off_are_unchanged_without_gpu_light(self):
        with mock.patch.dict(os.environ, {"HAL_GPU_LIGHT": "off"}):
            on, off = power.on(quiet=True), power.off(quiet=True)
        self.assertEqual(self.calls(), ["systemctl start hal.target"])
        self.assertNotIn("Warning", on + off)

    def test_a_failing_gpu_light_is_a_warning_not_an_error(self):
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_CLAIM_EXIT": "1"}):
            msg = power.on(quiet=True)
        self.assertIn("systemctl start hal.target", self.calls())
        self.assertIn("gpu-light claim failed (exit 1)", msg)

    def test_status_shows_the_light_and_its_reason(self):
        doc = self.dir / "status.json"
        doc.write_text(json.dumps({"effective": "red", "reason": "claim hal (boot)"}))
        with mock.patch.dict(os.environ, {"FAKE_GPU_LIGHT_STATUS": str(doc)}):
            self.assertIn("GPU light: red (claim hal (boot))", power.status())


if __name__ == "__main__":
    unittest.main()

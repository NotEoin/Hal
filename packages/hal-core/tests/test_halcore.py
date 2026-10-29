"""Tests for halcore: the brain's tool loop and confirmation gate, the eval checks, units and config edits.

No model or network: the model's replies are scripted and hal-hub is a fake.
Run with: uv run pytest packages/hal-core
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repository root
sys.path.insert(0, str(ROOT / "packages" / "hal-core" / "src"))
TMP = tempfile.mkdtemp(prefix="halcore-test-")
os.environ.update(HAL_CONFIG=f"{TMP}/config.toml", HAL_STATE=f"{TMP}/state", HAL_DATA=f"{TMP}/data")

from halcore import brain as brainmod, evaluate, ext, units  # noqa: E402


class FakeTool:
    def __init__(self, name, confirm=False):
        self.name, self.confirm = name, confirm


class FakeHub:
    """Records calls; tools named in `confirm` ask for confirmation unless _confirmed."""

    def __init__(self, confirm=()):
        self.calls, self.confirm = [], set(confirm)

    def get(self, name):
        return FakeTool(name, name in self.confirm)

    def ollama_tools(self, text, limit):
        return [{"type": "function", "function": {"name": n, "description": "", "parameters":
                 {"type": "object", "properties": {}}}} for n in ("calendar_add", "calendar_delete")]

    def call(self, name, args):
        self.calls.append((name, dict(args)))
        if name in self.confirm and not args.get("_confirmed"):
            return {"needs_confirmation": name}
        if name == "memory_profile":
            return {"result": ""}
        return {"result": {"ok": name}}

    def all_tools(self):
        return {}


def scripted(*rounds):
    """Returns a _stream_round replacement that plays back (content, calls) per model call."""
    queue = list(rounds)

    def stream(self, messages, tools):
        content, calls = queue.pop(0)
        if content:
            yield "text", content
        yield "done", (content, calls)
    return stream


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class BrainTest(unittest.TestCase):
    def make(self, hub, *rounds):
        b = brainmod.Brain(tools=False, log_turns=False)
        b.hub = hub
        b._stream_round = scripted(*rounds).__get__(b)
        return b

    def test_tool_then_reply(self):
        hub = FakeHub()
        b = self.make(hub, ("", [call("calendar_add", title="Dentist")]), ("Added the dentist.", []))
        self.assertEqual(b.reply("dentist thursday"), "Added the dentist.")
        self.assertEqual(b.last_turn["tools"][0]["name"], "calendar_add")

    def test_self_confirmation_is_refused(self):
        hub = FakeHub(confirm=["calendar_delete"])
        b = self.make(hub, ("", [call("calendar_delete", event="x", _confirmed=True)]), ("Are you sure?", []))
        b.reply("delete x")
        self.assertNotIn("_confirmed", hub.calls[-1][1])
        self.assertTrue(b.last_turn["tools"][0]["confirm"])

    def test_confirmation_after_asking_is_honoured(self):
        hub = FakeHub(confirm=["calendar_delete"])
        b = self.make(hub, ("", [call("calendar_delete", event="x")]), ("Are you sure?", []),
                      ("", [call("calendar_delete", event="x", _confirmed=True)]), ("Deleted.", []))
        b.reply("delete x")
        b.reply("yes")
        self.assertTrue(hub.calls[-1][1].get("_confirmed"))
        self.assertFalse(b.last_turn["tools"][0]["confirm"])

    def test_claimed_action_without_tool_is_retried(self):
        hub = FakeHub()
        b = self.make(hub, ("I've added it to your calendar.", []), ("", [call("calendar_add", title="x")]),
                      ("Done: it's in the calendar.", []))
        b.reply("lunch friday at 1")
        self.assertTrue(b.last_turn["retried"])
        self.assertEqual([c[0] for c in hub.calls if c[0] != "memory_profile"], ["calendar_add"])

    def test_leaked_tool_call_text_is_run(self):
        hub = FakeHub()
        b = self.make(hub, ('calendar_add{"title": "Gym"}', []), ("Added gym.", []))
        self.assertEqual(b.reply("gym"), "Added gym.")
        self.assertEqual(hub.calls[-1][0], "calendar_add")

    def test_raw_hal_claim_is_retried(self):
        b = brainmod.Brain(tools=False, log_turns=False)
        b._stream_round = scripted(("I've added it to your calendar.", []),
                                   ("I can't do that here: add the calendar extension.", [])).__get__(b)
        self.assertIn("calendar extension", b.reply("add lunch friday"))
        self.assertTrue(b.last_turn["retried"])

    def test_no_extensions_prompt_is_honest(self):
        b = brainmod.Brain(tools=False, log_turns=False)
        self.assertIn("extension", b.system_prompt())
        self.assertEqual([t["function"]["name"] for t in b.tools_for("hi")], ["deep_work"])


class EvalTest(unittest.TestCase):
    def test_placeholders(self):
        sat = date(2026, 10, 10)
        self.assertEqual(evaluate.fill("{date:thursday}T10:00", sat), "2026-10-15T10:00")
        self.assertEqual(evaluate.fill("{date:tomorrow}", sat), "2026-10-11")
        self.assertEqual(evaluate.fill("{weekday:today}", sat), "Saturday")
        self.assertEqual(evaluate.fill("{day:saturday}", sat), "17")

    def test_checks(self):
        case = {"tools": ["calendar_add"], "args": {"calendar_add": {"title": "(?i)dentist"}},
                "reply": "(?i)thursday", "max_chars": 50}
        good = [{"tools": [{"name": "calendar_add", "args": {"title": "Dentist"}, "confirm": False}]}]
        self.assertEqual(evaluate.check(case, good, None, "Booked for Thursday."), [])
        bad = [{"tools": []}]
        problems = evaluate.check(case, bad, None, "x" * 60)
        self.assertEqual(len(problems), 3)

    def test_confirm_first(self):
        case = {"confirm_first": ["system_kill"]}
        unasked = [{"tools": [{"name": "system_kill", "args": {}, "confirm": False}]}]
        self.assertTrue(evaluate.check(case, unasked, None, "done"))

    def test_scenarios_parse(self):
        cases = evaluate.load_cases()
        self.assertGreater(len(cases), 30)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for c in cases:
            self.assertTrue(c.get("ask") or c.get("turns"), c["id"])


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


class ExtTest(unittest.TestCase):
    def test_set_disabled_keeps_the_rest(self):
        Path(os.environ["HAL_CONFIG"]).write_text('[hal]\nvault = "v"\n\n[hub]\npinned = ["a"]\n')
        ext.set_disabled(["phone"])
        ext.set_disabled(["phone", "mail"])
        text = Path(os.environ["HAL_CONFIG"]).read_text()
        self.assertIn('disabled = ["mail", "phone"]', text)
        self.assertIn('pinned = ["a"]', text)
        self.assertEqual(text.count("disabled"), 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for `halcore.brain`: the tool loop and the confirmation gate.

No model or network: the model's replies are scripted and hal-hub is a fake.
Run with: uv run pytest packages/hal-core
"""
import unittest
from unittest import mock

import halcore_env  # noqa: F401  (sets up the import path and a throwaway Hal state)
from halcore import brain as brainmod


class FakeTool:
    def __init__(self, name, confirm=False):
        self.name, self.confirm = name, confirm


class FakeHub:
    """Records calls; tools named in `confirm` ask for confirmation unless _confirmed."""

    def __init__(self, confirm=()):
        self.calls, self.confirm = [], set(confirm)

    def get(self, name):
        return FakeTool(name, name in self.confirm)

    def function_specs(self, text, limit):
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
        self.assertEqual([t["function"]["name"] for t in b.tools_for("hi")], [])
        b.deep_mode = "hal"
        self.assertEqual([t["function"]["name"] for t in b.tools_for("hi")], ["deep_work"])

    def test_long_jobs_are_off_by_default_and_never_start_claude_code(self):
        b = brainmod.Brain(tools=False, log_turns=False)
        self.assertEqual(b.deep_mode, "off")
        with mock.patch.object(brainmod.subprocess, "run") as run:
            self.assertIn("error", b._deep_work("refactor everything"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

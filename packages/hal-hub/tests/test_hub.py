"""Tests for discovery, search, calling and the MCP server, using a throwaway module."""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class HubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        mod = Path(cls.tmp.name) / "hal-demo"
        (mod / "demo").mkdir(parents=True)
        (mod / "demo" / "__init__.py").write_text("")
        (mod / "demo" / "tools.py").write_text(textwrap.dedent('''
            def add(a, b):
                return a + b
            def wipe(path):
                return f"wiped {path}"
        '''))
        (mod / "hal-module.toml").write_text(textwrap.dedent('''
            [module]
            name = "demo"
            summary = "test module"
            python = "demo.tools"
            [[tools]]
            name = "demo_add"
            function = "add"
            description = "Add two numbers together"
            [tools.params.a]
            type = "integer"
            required = true
            [tools.params.b]
            type = "integer"
            required = true
            [[tools]]
            name = "demo_wipe"
            function = "wipe"
            description = "Delete a folder for good"
            confirm = true
            [tools.params.path]
            required = true
        '''))
        os.environ["HAL_MODULE_PATH"] = f"{cls.tmp.name}:{ROOT}"
        os.environ["HAL_STATE"] = cls.tmp.name
        import halhub
        cls.hub = halhub
        halhub.all_tools(refresh=True)

    def test_discovers_modules(self):
        self.assertIn("demo_add", self.hub.all_tools())
        self.assertIn("hal_find_tools", self.hub.all_tools())

    def test_search_ranks_relevant_tool_first(self):
        self.assertEqual(self.hub.find("add numbers")[0].name, "demo_add")

    def test_call_coerces_strings(self):
        self.assertEqual(self.hub.call("demo_add", {"a": "2", "b": "3"}), {"result": 5})

    def test_missing_and_unknown_arguments_are_errors(self):
        self.assertIn("needs", self.hub.call("demo_add", {"a": 1})["error"])
        self.assertIn("no parameter", self.hub.call("demo_add", {"a": 1, "b": 2, "c": 3})["error"])

    def test_confirm_tools_wait_for_confirmation(self):
        self.assertIn("needs_confirmation", self.hub.call("demo_wipe", {"path": "/x"}))
        self.assertEqual(self.hub.call("demo_wipe", {"path": "/x", "_confirmed": True}), {"result": "wiped /x"})

    def test_mcp_lists_and_calls(self):
        from halhub import mcp
        listed = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("hal_call", [t["name"] for t in listed["result"]["tools"]])
        reply = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "hal_call", "arguments": {"tool": "demo_add", "arguments": {"a": 1, "b": 1}}}})
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"]), {"result": 2})

    def test_ollama_specs_include_core_search(self):
        names = [s["function"]["name"] for s in self.hub.ollama_tools("add two numbers")]
        self.assertIn("hal_find_tools", names)
        self.assertIn("demo_add", names)


if __name__ == "__main__":
    unittest.main()

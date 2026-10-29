"""Tests for the module-level `halhub` API and `halhub.api` (REQ-HUB-61, REQ-HUB-62).

Ported from the v1 `test_hub.py`: discovery, search, calling with coercion and the old
confirmation flow, now on the hub facade. The `_confirmed` retry is gone: an `act` tool asks
a host surface, and with none it is denied.
"""

from __future__ import annotations

import subprocess
import sys
import warnings

import pytest
from hubkit import SpyRunner

import halhub
from halhub import api

SUBPROCESS_TIMEOUT_S = 60  # a cold interpreter importing the package; generous for slow CI


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    """Each test sees the deprecation warnings as a new process would."""
    monkeypatch.setattr(api, "_warned", set())


def test_import_halhub_does_not_import_mcp():
    code = "import sys, halhub; halhub.function_specs; import halhub.api; print('mcp' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=True
    )
    assert out.stdout.strip() == "False"


def test_all_tools_has_builtin_and_pack_tools(process_hub):
    process_hub()
    tools = halhub.all_tools()
    assert "demo_echo" in tools and "hal_find_tools" in tools
    assert halhub.get("demo_echo") is tools["demo_echo"]


def test_find_ranks_the_relevant_tool_first(process_hub):
    process_hub()
    assert halhub.find("pretend to delete a file")[0].name == "demo_wipe"


def test_call_returns_v1_result_key_and_coerces_strings(process_hub):
    spy = SpyRunner(result=lambda tool, args: args["times"])
    process_hub(runner=spy)
    with pytest.warns(DeprecationWarning, match="halhub.client.call"):
        out = halhub.call("demo_echo", {"text": "hi", "times": "2"})
    assert out["status"] == "ok" and out["result"] == 2


def test_call_warns_only_once_per_process(process_hub):
    process_hub()
    with pytest.warns(DeprecationWarning):
        halhub.call("demo_echo", {"text": "a"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        halhub.call("demo_echo", {"text": "b"})
    assert caught == []


def test_call_failure_has_v1_error_key_and_status(process_hub):
    process_hub()
    with pytest.warns(DeprecationWarning):
        unknown = halhub.call("no_such_tool", {})
    missing = halhub.call("demo_echo", {})
    assert unknown["status"] == "error" and "hal_find_tools" in unknown["error"]
    assert missing["status"] == "error" and "text" in missing["error"]


def test_call_to_act_tool_without_approver_is_denied_even_when_confirmed(process_hub):
    spy = SpyRunner()
    process_hub(runner=spy)
    with pytest.warns(DeprecationWarning):
        out = halhub.call("demo_wipe", {"path": "~/Downloads/x", "_confirmed": True})
    assert out["status"] == "denied" and out["rule"] == "no-approver"
    assert "needs_confirmation" not in out and spy.calls == []


def test_function_specs_start_with_always_offer_tools(process_hub):
    process_hub()
    names = [s["function"]["name"] for s in halhub.function_specs("pretend to delete a file")]
    assert names[:2] == ["demo_echo", "hal_find_tools"] and "demo_wipe" in names


def test_ollama_tools_is_a_deprecated_alias_of_function_specs(process_hub):
    process_hub()
    with pytest.warns(DeprecationWarning, match="function_specs"):
        old = halhub.ollama_tools("echo")
    assert old == halhub.function_specs("echo")


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="CORE"):
        halhub.CORE  # noqa: B018 - the attribute access is the test


def test_every_lazy_name_is_exported():
    assert set(halhub.__all__) == set(halhub._LAZY)
    assert halhub.default_hub is api.default_hub

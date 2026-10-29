"""Tests for `ToolSpec.meta`: the `ToolMeta` that 01-security's policy reads (REQ-HUB-02, REQ-HUB-17).

`test_annotations_are_ignored_by_decide` (named by 01-security) needs 01's `decide` and lands
with the call pipeline (04-hub plan step 10); until then `ToolMeta` comes from the phase-1
stand-in in `halhub._policy_standin`.
"""

from __future__ import annotations

import textwrap

import pytest

from halhub._policy_standin import Tier
from halhub.manifest import load_manifest

V1 = """
[module]
name = "old"
summary = "a third-party v1 module"
python = "old.tools"
[[tools]]
name = "old_reboot"
description = "Reboot."
confirm = {confirm}
"""


@pytest.mark.parametrize("confirm", ["true", "false"])
def test_v1_tool_without_tier_registers_as_act(tmp_path, confirm):
    path = tmp_path / "hal-module.toml"
    path.write_text(textwrap.dedent(V1.format(confirm=confirm)))
    module = load_manifest(path, source="path")
    tool = module.tools[0]
    meta = tool.meta(module)
    assert tool.tier == "act" and tool.tier_inferred
    assert meta.tier is Tier.ACT and meta.reviewable is False


def test_v1_tool_with_no_confirm_key_registers_as_act(tmp_path):
    path = tmp_path / "hal-module.toml"
    path.write_text(textwrap.dedent(V1.replace("confirm = {confirm}\n", "")))
    module = load_manifest(path, source="path")
    assert module.tools[0].meta(module).tier is Tier.ACT


def test_tool_meta_carries_format_access_and_reversible(fixtures):
    module = load_manifest(fixtures / "demo" / "hal-module.toml", source="path")
    tools = {t.name: t for t in module.tools}
    wipe = tools["demo_wipe"].meta(module)
    assert wipe.params[0].format == "path" and wipe.params[0].access == "write"
    assert wipe.params[0].max_length == 4096 and wipe.params[0].required
    lights = tools["demo_lights"].meta(module)
    assert lights.reversible and lights.reviewable and lights.tier is Tier.ACT
    assert lights.params[0].enum == ("red", "green", "blue")
    fetch = tools["demo_fetch"].meta(module)
    assert fetch.untrusted_output and fetch.network and fetch.params[0].format == "url"


def test_tool_meta_carries_pack_version_and_domains(tmp_path):
    path = tmp_path / "hal-module.toml"
    path.write_text(
        textwrap.dedent(
            """
            schema = 2
            [module]
            name = "web"
            pack = "online"
            summary = "s"
            version = "0.1.0"
            python = "w.tools"
            domains = ["*.example.org"]
            [[tools]]
            name = "web_read"
            description = "Read a page."
            tier = "send"
            runs_code = false
            [tools.params.body]
            content = true
            """
        )
    )
    module = load_manifest(path, source="path")
    meta = module.tools[0].meta(module)
    assert (meta.pack, meta.pack_version, meta.domains) == ("online", "0.1.0", ("*.example.org",))
    assert meta.tier is Tier.SEND and meta.params[0].content

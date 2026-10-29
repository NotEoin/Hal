"""The shared approval vectors through the hub's normalisation and 01's key (REQ-HUB-74).

`tests/data/approval_vectors.json` at the repository root is read by the tests of 01, 04 and
06 (RESOLUTIONS B1). Each vector's arguments go through `args.normalise` for a tool declared
with matching parameters, then through `decide`, whose key must equal the vector's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from halhub.approvals import approval_key
from halhub.args import normalise
from halhub.manifest import ModuleSpec, ParamSpec, ToolSpec
from halhub.policy import (
    PROFILES,
    Context,
    Origin,
    Policy,
    RateSnapshot,
    Taint,
    ToolCall,
    decide,
)
from halkit.canon import canonical_json
from halkit.testing import FakeClock

VECTORS = Path(__file__).resolve().parents[3] / "tests" / "data" / "approval_vectors.json"
_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string"}


def _vectors() -> list[dict]:
    return json.loads(VECTORS.read_text())


def _tool(vector: dict) -> tuple[ToolSpec, ModuleSpec]:
    params = tuple(
        ParamSpec(name=name, type=_TYPES[type(value)], required=True)
        for name, value in vector["args"].items()
    )
    tool = ToolSpec(
        name=vector["tool"],
        module="vectors",
        pack=vector["pack"],
        function="f",
        description="d",
        tier="act",
        tier_inferred=False,
        params=params,
    )
    module = ModuleSpec(
        name="vectors",
        pack=vector["pack"],
        summary="s",
        version="0.1.0",
        schema=2,
        root=VECTORS.parent,
        manifest=VECTORS,
        source="path",
        distribution=None,
        python="m",
        exec=None,
        protocol="jsonl",
        interpreter=None,
        inprocess=False,
        requires=(),
        calls=(),
        requires_hal=None,
        env=(),
        domains=(),
        secrets=(),
        startup_timeout=20.0,
        max_workers=2,
        keywords=(),
        tools=(tool,),
        install=(),
        units=(),
        desktop=(),
        warnings=(),
    )
    return tool, module


def test_vectors_file_is_not_empty():
    assert len(_vectors()) >= 4


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: f"{v['tool']}:{v['key'][7:15]}")
def test_normalised_vector_reproduces_canonical_form_and_key(vector):
    tool, module = _tool(vector)
    args = normalise(tool, vector["args"]).args
    assert canonical_json(args).decode("ascii") == vector["canonical"]
    assert approval_key(vector["pack"], vector["tool"], args) == vector["key"]
    call = ToolCall(vector["tool"], args, "0123456789ab", Origin.CLI, "c_0000000000000001")
    ctx = Context(FakeClock().now(), "hub", Taint.UNTRUSTED, (), RateSnapshot(), True, {}, (), Path("/"))
    assert decide(call, tool.meta(module), Policy(profile=PROFILES["default"]), ctx).key == vector["key"]

"""Tests for approval keys and tool security digests (`halhub.approvals.keys`, REQ-SEC-19, REQ-SEC-21)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from halhub.approvals.keys import approval_key, security_digest
from halhub.policy.model import ParamSpec, Tier, ToolMeta
from halkit.canon import canonical_json

VECTORS = Path(__file__).resolve().parents[3] / "tests" / "data" / "approval_vectors.json"
MAIL = ToolMeta(
    "mail_send",
    "comms",
    "0.1.0",
    Tier.SEND,
    (ParamSpec("to", required=True), ParamSpec("body", content=True)),
    network=True,
    domains=("b.example", "a.example"),
)


@pytest.mark.parametrize("vector", json.loads(VECTORS.read_text()), ids=lambda v: v["key"][7:15])
def test_shared_vectors_reproduce_canonical_form_and_key(vector):
    assert canonical_json(vector["args"]).decode("ascii") == vector["canonical"]
    assert approval_key(vector["pack"], vector["tool"], vector["args"]) == vector["key"]


def test_pack_name_is_part_of_the_key():
    args = {"id": 42}
    assert approval_key("organiser", "calendar_delete", args) != approval_key(
        "other", "calendar_delete", args
    )


def test_key_distinguishes_bool_from_int():
    assert approval_key("p", "t", {"x": True}) != approval_key("p", "t", {"x": 1})


def test_digest_ignores_pack_version_and_param_order():
    reordered = replace(MAIL, pack_version="9.9.9", params=MAIL.params[::-1], domains=MAIL.domains[::-1])
    assert security_digest(reordered) == security_digest(MAIL)


@pytest.mark.parametrize(
    "change",
    [
        {"tier": Tier.ACT},
        {"network": False},
        {"untrusted_output": True},
        {"reviewable": True},
        {"reversible": True},
        {"runs_code": True},
        {"domains": ("a.example",)},
        {"params": (ParamSpec("to", required=True), ParamSpec("body"))},
        {"params": (ParamSpec("to", required=True, format="email"), ParamSpec("body", content=True))},
        {"params": (ParamSpec("to", required=True, max_length=10), ParamSpec("body", content=True))},
        {"params": (ParamSpec("to", required=True, enum=("a",)), ParamSpec("body", content=True))},
    ],
    ids=lambda c: next(iter(c)),
)
def test_digest_changes_with_every_security_field(change):
    assert security_digest(replace(MAIL, **change)) != security_digest(MAIL)


def test_digest_has_the_sha256_prefix():
    digest = security_digest(MAIL)
    assert digest.startswith("sha256:") and len(digest) == 7 + 64

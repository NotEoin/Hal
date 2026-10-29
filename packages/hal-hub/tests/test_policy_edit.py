"""Tests for `halhub.policy.edit`: `allow_rule` for `hal policy allow` (01 REQ-SEC-28)."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from halhub.policy import Context, Origin, RateSnapshot, Taint, ToolCall, Verdict, decide, load_policy
from halhub.policy.edit import PolicyEditError, allow_rule
from halhub.policy.model import ParamSpec, Tier, ToolMeta
from halkit.config import ConfigError

MAIL = ToolMeta(
    name="mail_send",
    pack="comms",
    pack_version="0.1.0",
    tier=Tier.SEND,
    params=(ParamSpec("to", required=True, format="email"), ParamSpec("subject")),
)
CALENDAR = ToolMeta(
    name="calendar_delete",
    pack="organiser",
    pack_version="0.1.0",
    tier=Tier.ACT,
    params=(ParamSpec("id", "integer", required=True),),
)
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(text)
    path.chmod(0o600)
    return path


def tools(text: str) -> dict:
    return tomllib.loads(text).get("tools", {})


def verdict(text: str, tmp_path: Path, meta: ToolMeta, args: dict) -> Verdict:
    policy = load_policy(write(tmp_path, text))
    call = ToolCall(meta.name, args, "0123456789ab", Origin.TUI, "c_" + "0" * 16)
    ctx = Context(NOW, "chat", Taint.CLEAN, (), RateSnapshot(), True, {}, (), tmp_path)
    return decide(call, meta, policy, ctx).verdict


def test_allow_rule_for_system_tool_is_refused(tmp_path):
    shutdown = ToolMeta(
        name="power_shutdown", pack="system", pack_version="0.1.0", tier=Tier.SYSTEM, params=()
    )
    with pytest.raises(PolicyEditError, match="REQ-SEC-08"):
        allow_rule(tmp_path / "policy.toml", "power_shutdown", args={}, meta=shutdown)


def test_allow_rule_for_runs_code_tool_is_refused(tmp_path):
    runner = ToolMeta(
        name="test_run", pack="work", pack_version="0.1.0", tier=Tier.ACT, params=(), runs_code=True
    )
    with pytest.raises(PolicyEditError, match="always ask"):
        allow_rule(tmp_path / "policy.toml", "test_run", domain="example.org", meta=runner)


def test_file_without_schema_version_gets_one(tmp_path):
    text = allow_rule(write(tmp_path, "# empty\n"), "calendar_delete", args={"id": 1})
    assert text.startswith("schema_version = 1\n# empty\n")


def test_missing_file_gets_a_schema_version_and_a_new_table(tmp_path):
    text = allow_rule(tmp_path / "absent.toml", "calendar_delete", args={"id": 42}, meta=CALENDAR)
    assert tomllib.loads(text)["schema_version"] == 1
    assert tools(text) == {"calendar_delete": {"allow_args": {"id": ["42"]}}}
    assert not (tmp_path / "absent.toml").exists()  # allow_rule writes nothing


def test_rule_turns_the_exact_call_from_ask_into_allow_and_nothing_else(tmp_path):
    args = {"to": "ann@example.org", "subject": "Notes"}
    text = allow_rule(tmp_path / "absent.toml", "mail_send", args=args, meta=MAIL)
    assert verdict(text, tmp_path, MAIL, args) is Verdict.ALLOW
    assert verdict(text, tmp_path, MAIL, {**args, "to": "bob@example.org"}) is Verdict.ASK


def test_glob_characters_in_a_value_match_only_themselves(tmp_path):
    meta = ToolMeta(
        name="files_trash", pack="desktop", pack_version="0.1.0", tier=Tier.ACT, params=(ParamSpec("name"),)
    )
    text = allow_rule(tmp_path / "absent.toml", "files_trash", args={"name": "report[1]*.txt"}, meta=meta)
    assert verdict(text, tmp_path, meta, {"name": "report[1]*.txt"}) is Verdict.ALLOW
    assert verdict(text, tmp_path, meta, {"name": "report1-final.txt"}) is Verdict.ASK


def test_comments_and_other_tables_are_kept(tmp_path):
    original = "schema_version = 1\n# my notes\n[egress]\ndomains = ['example.org']  # docs\n"
    text = allow_rule(write(tmp_path, original), "calendar_delete", args={"id": 7})
    assert text.startswith(original) and "[tools.calendar_delete]" in text


def test_key_is_added_inside_an_existing_table(tmp_path):
    original = "schema_version = 1\n[tools.mail_send]\nrate = '5/h'\n\n[rate]\nsend = '10/h'\n"
    text = allow_rule(write(tmp_path, original), "mail_send", domain="Example.ORG")
    assert tools(text)["mail_send"] == {"rate": "5/h", "domains": ["example.org"]}
    assert tomllib.loads(text)["rate"] == {"send": "10/h"}


def test_single_parameter_rules_are_merged(tmp_path):
    first = allow_rule(write(tmp_path, ""), "calendar_delete", args={"id": 1})
    second = allow_rule(write(tmp_path, first), "calendar_delete", args={"id": 2})
    assert tools(second)["calendar_delete"]["allow_args"] == {"id": ["1", "2"]}


def test_rule_already_present_leaves_the_text_unchanged(tmp_path):
    first = allow_rule(write(tmp_path, ""), "calendar_delete", args={"id": 1})
    assert allow_rule(write(tmp_path, first), "calendar_delete", args={"id": 1}) == first
    domains = allow_rule(write(tmp_path, ""), "mail_send", domain="example.org")
    assert allow_rule(write(tmp_path, domains), "mail_send", domain="example.org") == domains


def test_multi_parameter_merge_is_refused(tmp_path):
    first = allow_rule(write(tmp_path, ""), "mail_send", args={"to": "a@example.org", "subject": "x"})
    with pytest.raises(PolicyEditError, match="combinations"):
        allow_rule(write(tmp_path, first), "mail_send", args={"to": "b@example.org", "subject": "y"})


@pytest.mark.parametrize(
    "original",
    [
        "schema_version = 1\n[tools.mail_send]\ndomains = [\n  'a.example',\n]\n",
        "schema_version = 1\n[tools.mail_send.domains]\n",
        "schema_version = 1\ntools.mail_send.rate = '5/h'\n",
    ],
    ids=["multi-line", "sub-table", "dotted"],
)
def test_layouts_the_line_editor_cannot_change_need_a_hand_edit(tmp_path, original):
    with pytest.raises((PolicyEditError, tomllib.TOMLDecodeError)):
        allow_rule(write(tmp_path, original), "mail_send", domain="b.example")


def test_unknown_argument_is_refused(tmp_path):
    with pytest.raises(PolicyEditError, match="no argument"):
        allow_rule(tmp_path / "absent.toml", "calendar_delete", args={"ids": 1}, meta=CALENDAR)


def test_exactly_one_of_args_or_domain_is_needed(tmp_path):
    with pytest.raises(PolicyEditError, match="exactly one"):
        allow_rule(tmp_path / "absent.toml", "mail_send")
    with pytest.raises(PolicyEditError, match="exactly one"):
        allow_rule(tmp_path / "absent.toml", "mail_send", args={"to": "a"}, domain="example.org")


def test_bad_domain_fails_the_load_check(tmp_path):
    with pytest.raises(ConfigError, match="host name"):
        allow_rule(tmp_path / "absent.toml", "mail_send", domain="not a host")


def test_non_ascii_and_control_characters_are_written_as_escapes(tmp_path):
    meta = ToolMeta(
        name="notes_tag", pack="life", pack_version="0.1.0", tier=Tier.ACT, params=(ParamSpec("tag"),)
    )
    value = "caf\u00e9\n\U0001f600"
    text = allow_rule(tmp_path / "absent.toml", "notes_tag", args={"tag": value}, meta=meta)
    assert text.isascii() and tools(text)["notes_tag"]["allow_args"] == {"tag": [value]}


def test_bad_tool_name_is_refused(tmp_path):
    with pytest.raises(PolicyEditError, match="tool name"):
        allow_rule(tmp_path / "absent.toml", "x]\n[profiles", domain="example.org")

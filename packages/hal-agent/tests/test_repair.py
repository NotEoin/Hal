"""Tests for `halagent.repair`: leak parsers, name and argument aliases, coercion, fuzz."""

import random

import pytest

from halagent.repair import repair_call, repair_text
from halagent.repair.aliases import parse_args, resolve_name
from halagent.repair.leaks import has_open_marker, parse_gemma_args, parse_leaks
from halagent.tools import Toolset

OFFERED = {
    n: {"properties": t.properties, "required": t.required}
    for n, t in Toolset(".", task_mode=True).tools.items()
}


def one(text):
    parser, calls, rest = parse_leaks(text)
    return parser, [(c.name, c.args) for c in calls], rest


def test_gemma_call_with_bare_keys_and_quoted_strings_is_parsed():
    text = 'hi <|tool_call>call:edit{path:<|"|>a.py<|"|>,n:3,flag:true,obj:{k:<|"|>v<|"|>}}<tool_call|>'
    parser, calls, rest = one(text)
    assert parser == "gemma4" and rest == "hi"
    assert calls == [("edit", {"path": "a.py", "n": 3, "flag": True, "obj": {"k": "v"}})]


def test_hermes_json_call_is_parsed():
    text = '<tool_call>{"name": "read", "arguments": {"path": "a"}}</tool_call>'
    assert one(text)[:2] == ("hermes", [("read", {"path": "a"})])


def test_qwen_xml_call_is_parsed():
    text = "<tool_call><function=run><parameter=command>\nls -l\n</parameter></function></tool_call>"
    assert one(text)[:2] == ("qwen_xml", [("run", {"command": "ls -l"})])


def test_mistral_call_and_list_forms_are_parsed():
    assert one('[TOOL_CALLS]read[ARGS]{"path": "a"}')[1] == [("read", '{"path": "a"}')]
    assert one('[TOOL_CALLS] [{"name": "grep", "arguments": {"pattern": "x"}}]')[1] == [
        ("grep", {"pattern": "x"})
    ]


def test_harmony_call_is_parsed():
    text = '<|channel|>commentary to=functions.read <|constrain|>json<|message|>{"path": "a"}<|call|>'
    assert one(text)[:2] == ("harmony", [("read", '{"path": "a"}')])


def test_glm_call_is_parsed():
    text = "<tool_call>read<arg_key>path</arg_key><arg_value>a.py</arg_value></tool_call>"
    assert one(text)[:2] == ("glm", [("read", {"path": "a.py"})])


def test_whole_content_name_json_and_fenced_json_are_parsed():
    assert one('read {"path": "a"}')[0] == "name_json"
    assert one('```json\n{"name": "read", "parameters": {"path": "a"}}\n```')[1] == [("read", {"path": "a"})]


def test_plain_prose_yields_no_call():
    assert one("I will read the file now.")[1] == []


def test_parser_skips_calls_to_tools_that_are_not_offered():
    text = '<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>'
    assert repair_text(text, OFFERED)[1] == []


def test_truncated_call_markers_are_detected():
    assert has_open_marker('<tool_call>{"name": "read", "argu')
    assert not has_open_marker('<tool_call>{"name": "read"}</tool_call>')


def test_parsers_survive_random_garbage_quickly():
    rng = random.Random(7)  # fixed seed: the fuzz must be deterministic
    alphabet = ["<|tool_call>", "call:", "{", "}", "[", "]", '<|"|>', ",", ":", "<tool_call|>"]
    alphabet += ["[TOOL_CALLS]", "[ARGS]", "<tool_call>", "</tool_call>", "a", "1", " "]
    alphabet += ["<parameter=", "<arg_key>"]
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60)))
        parse_leaks(text)
        has_open_marker(text)


def test_gemma_argument_parser_rejects_deep_nesting():
    with pytest.raises(ValueError):
        parse_gemma_args("a:" + "[" * 100 + "]" * 100)


@pytest.mark.parametrize(
    ("given", "want"),
    [("read_file", "read"), ("Bash", "run"), ("functions.grep", "grep"), ("str_replace_editor", "edit"),
     ("submit", "finish"), ("deploy_site", None)],
)  # fmt: skip
def test_names_resolve_through_case_prefix_and_alias(given, want):
    assert resolve_name(given, OFFERED) == want


def test_editor_command_view_maps_to_read():
    assert resolve_name("str_replace_editor", OFFERED, {"command": "view"}) == "read"


def test_alias_is_ignored_when_target_is_not_offered():
    assert resolve_name("bash", {"read": {}}) is None


def test_arguments_parse_through_trailing_commas_python_literals_and_double_encoding():
    assert parse_args('{"a": 1,}') == {"a": 1}
    assert parse_args("{'a': True}") == {"a": True}
    assert parse_args('"{\\"a\\": 1}"') == {"a": 1}
    assert parse_args("") == {} and parse_args("[1]") is None


def test_argument_aliases_and_coercion_apply_by_schema():
    r = repair_call("read_file", '{"file_path": "a", "offset": "5", "limit": 10, "confirmed": true}', OFFERED)
    assert (r.name, r.args) == ("read", {"path": "a", "start_line": 5, "end_line": 14})
    r = repair_call("shell", {"cmd": ["ls -l", "pwd"]}, OFFERED)
    assert r.args == {"command": "ls -l && pwd"}
    r = repair_call("update_plan", {"todos": ["one", "- [x] two"]}, OFFERED)
    assert r.args == {"plan": "- [ ] one\n- [x] two"}


def test_unknown_argument_is_dropped_with_a_note_and_missing_required_is_an_error():
    r = repair_call("read", {"path": "a", "foo": 1}, OFFERED)
    assert r.notes == ["[hal] ignored unknown argument 'foo'"]
    r = repair_call("edit", {"path": "a"}, OFFERED)
    assert r.error == ("invalid_args", "edit needs path, old_string, new_string; got path.")


def test_unknown_tool_lists_the_offered_tools():
    r = repair_call("deploy_site", {}, OFFERED)
    assert r.error and r.error[0] == "unknown_tool" and "Your tools are: read, grep" in r.error[1]

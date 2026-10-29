"""Tests for `halhub.args`: the coercion table row by row, aliases, near misses, lists, JSON.

REQ-HUB-13 (hub checks end the call), REQ-HUB-14 (coercion table), REQ-HUB-15 (aliases and
CLI-only near misses), REQ-HUB-16 (`_` keys untouched). Where 01's `decide` is the one that
rejects (an unknown name, a value outside `enum`), these tests check that the hub hands the
value on unchanged; the rejection itself is tested with the call pipeline (plan step 10).
"""

from __future__ import annotations

import math

import pytest

from halhub.args import MAX_ARGS_BYTES, ArgumentError, call_arguments, normalise
from halhub.manifest import ParamSpec, ToolSpec


def tool(*params: ParamSpec) -> ToolSpec:
    return ToolSpec(
        name="demo_x", module="demo", pack="demo", function="x", description="d", tier="read",
        tier_inferred=False, params=params,
    )  # fmt: skip


def one(value, **spec):
    """Normalises `{"p": value}` against one parameter declared with `spec`."""
    return normalise(tool(ParamSpec("p", **spec)), {"p": value}).args["p"]


def rejected(value, **spec) -> ArgumentError:
    with pytest.raises(ArgumentError) as info:
        one(value, **spec)
    assert info.value.param == "p"
    return info.value


# ---- string -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "expected"), [("a b", "a b"), (7, "7"), (2.5, "2.5"), ("", "")])
def test_string_accepts_strings_and_numbers(given, expected):
    assert one(given) == expected


@pytest.mark.parametrize("given", [True, None, [1], {"a": 1}])
def test_string_rejects_booleans_and_structures(given):
    assert rejected(given).code == "type"


# ---- integer ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"), [(3, 3), (4.0, 4), ("12", 12), ("-5", -5), ("+6", 6), (" 7 ", 7)]
)
def test_integer_accepts_ints_integral_floats_and_digit_strings(given, expected):
    result = one(given, type="integer")
    assert result == expected and type(result) is int


def test_boolean_is_not_an_integer():
    assert "boolean" in rejected(True, type="integer").message


@pytest.mark.parametrize("given", [4.5, "4.0", "four", "", float("inf"), None])
def test_integer_rejects_fractions_and_words(given):
    rejected(given, type="integer")


# ---- number -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "expected"), [(3, 3.0), (2.5, 2.5), ("1e3", 1000.0), (" -0.5 ", -0.5)])
def test_number_accepts_ints_floats_and_numeric_strings(given, expected):
    result = one(given, type="number")
    assert result == expected and type(result) is float


@pytest.mark.parametrize("given", [False, "nan", "inf", math.inf, "x", None])
def test_number_rejects_booleans_and_non_finite_values(given):
    rejected(given, type="number")


# ---- boolean ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [(True, True), (False, False), ("TRUE", True), ("no", False), ("On", True), ("off", False), ("1", True),
     ("0", False), (1, True), (0, False), ("yes", True)],
)  # fmt: skip
def test_boolean_accepts_listed_words_and_zero_one(given, expected):
    assert one(given, type="boolean") is expected


@pytest.mark.parametrize("given", ["maybe", "", 2, 1.0, None])
def test_unknown_boolean_word_is_rejected(given):
    rejected(given, type="boolean")


# ---- bounds and enum --------------------------------------------------------------------------


def test_bounds_are_inclusive():
    assert one(5, type="integer", minimum=1, maximum=5) == 5
    assert rejected(6, type="integer", minimum=1, maximum=5).code == "range"
    assert rejected(0.5, type="number", minimum=1).code == "range"


def test_enum_value_matching_only_without_case_is_mapped_and_reported():
    p = ParamSpec("colour", enum=("Red", "green"))
    result = normalise(tool(p), {"colour": "red"})
    assert result.args["colour"] == "Red" and "read as 'Red'" in result.warnings[0]


def test_value_outside_enum_is_left_for_decide():
    assert one("purple", enum=("red", "green")) == "purple"


# ---- lists ------------------------------------------------------------------------------------


def test_list_value_is_normalised_to_one_string():
    assert one(" a, b ,,c ", list=True) == "a,b,c"


def test_list_of_integers_is_checked_and_normalised():
    assert one("5, 015", list=True, item_type="integer") == "5,15"
    assert rejected("5, x", list=True, item_type="integer").code == "list"


def test_json_array_of_scalars_is_accepted_for_a_list():
    assert one(["a", 2], list=True) == "a,2"


@pytest.mark.parametrize("given", [["a,b"], [True], [["x"]], 5])
def test_list_rejects_items_with_commas_or_structure(given):
    assert rejected(given, list=True).code == "list"


def test_call_arguments_split_lists_by_item_type():
    t = tool(ParamSpec("tags", list=True), ParamSpec("mins", list=True, item_type="integer"), ParamSpec("x"))
    assert call_arguments(t, {"tags": "a,b", "mins": "5,15", "x": "a,b"}) == {
        "tags": ["a", "b"],
        "mins": [5, 15],
        "x": "a,b",
    }


def test_call_arguments_give_an_empty_list_for_an_empty_string():
    assert call_arguments(tool(ParamSpec("tags", list=True)), {"tags": ""}) == {"tags": []}


# ---- JSON format ------------------------------------------------------------------------------


def test_json_string_is_reserialised_canonically():
    assert one('{ "b": 1, "a": [true, null] }', format="json") == '{"a":[true,null],"b":1}'


def test_invalid_json_is_rejected():
    assert rejected("{nope", format="json").code == "json"


def test_object_is_accepted_only_for_v1_object_parameters():
    assert one({"b": 1, "a": 2}, format="json", v1_object=True) == '{"a":2,"b":1}'
    assert rejected({"a": 1}, format="json").code == "json"


def test_call_arguments_parse_v1_objects_only():
    t = tool(ParamSpec("old", format="json", v1_object=True), ParamSpec("new", format="json"))
    assert call_arguments(t, {"old": '{"a":1}', "new": '{"a":1}'}) == {"old": {"a": 1}, "new": '{"a":1}'}


# ---- names ------------------------------------------------------------------------------------


WHEN = tool(ParamSpec("when_text", required=True), ParamSpec("title"))


def test_declared_alias_applies_to_every_front_end():
    t = tool(ParamSpec("when_text", aliases=("when",)))
    for near_miss in (False, True):
        result = normalise(t, {"when": "thu"}, near_miss=near_miss)
        assert result.args == {"when_text": "thu"} and result.aliased == (("when", "when_text"),)
        assert result.warnings == ()


def test_near_miss_name_is_mapped_on_the_cli_only():
    cli = normalise(WHEN, {"when": "thu"}, near_miss=True)
    assert cli.args == {"when_text": "thu"}
    assert cli.warnings == ("argument 'when' read as 'when_text'",)
    model = normalise(WHEN, {"when": "thu"}, near_miss=False)
    assert model.args == {"when": "thu"} and model.aliased == ()  # 01's decide rejects it


def test_near_miss_with_target_present_is_not_mapped():
    result = normalise(WHEN, {"when": "thu", "when_text": "fri"}, near_miss=True)
    assert result.args == {"when": "thu", "when_text": "fri"} and result.aliased == ()


def test_ambiguous_near_miss_is_not_mapped():
    t = tool(ParamSpec("when_text"), ParamSpec("when_date"))
    assert normalise(t, {"when": "x"}, near_miss=True).args == {"when": "x"}


def test_near_miss_ignores_case_and_underscores():
    assert normalise(WHEN, {"WhenText": "x"}, near_miss=True).args == {"when_text": "x"}


def test_alias_and_name_together_are_a_duplicate():
    t = tool(ParamSpec("when_text", aliases=("when",)))
    with pytest.raises(ArgumentError) as info:
        normalise(t, {"when": "a", "when_text": "b"})
    assert info.value.code == "duplicate"


def test_unknown_name_is_passed_on_for_decide():
    assert normalise(WHEN, {"colour": "red"}).args == {"colour": "red"}


def test_underscore_arguments_reach_decide_untouched():
    t = tool(ParamSpec("n", type="integer"))
    result = normalise(t, {"_confirmed": True, "_x": "7", "n": "3"})
    assert result.args == {"_confirmed": True, "_x": "7", "n": 3}


# ---- shape and size ---------------------------------------------------------------------------


def test_missing_arguments_mean_an_empty_object():
    assert normalise(WHEN, None).args == {}


@pytest.mark.parametrize("raw", [["a"], "a=b", 3])
def test_non_object_arguments_are_rejected(raw):
    with pytest.raises(ArgumentError) as info:
        normalise(WHEN, raw)
    assert info.value.code == "shape" and info.value.param is None


def test_arguments_over_one_mebibyte_are_rejected():
    with pytest.raises(ArgumentError) as info:
        normalise(WHEN, {"title": "x" * MAX_ARGS_BYTES})
    assert info.value.code == "size"


def test_argument_error_names_tool_and_parameter():
    error = rejected("x", type="integer")
    assert str(error).startswith("demo_x.p: expected an integer")

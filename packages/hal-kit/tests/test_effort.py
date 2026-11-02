"""Tests for `halkit.effort`: the capability table and the mapping for each backend."""

import pytest

from halkit import effort


def cap(name, family=""):
    return effort.capability(family, name)


@pytest.mark.parametrize(
    ("name", "family", "kind"),
    [
        ("qwen3.5:4b", "", "toggle"),
        ("Qwen3.8-27B-UD-Q4_K_XL.gguf", "qwen3", "toggle"),
        ("anything", "qwen3moe", "toggle"),
        ("qwen3-coder:30b", "", "none"),
        ("deepseek-r1:14b", "", "always"),
        ("qwq:32b", "", "always"),
        ("gpt-oss:20b", "", "levels"),
        ("o3-mini", "", "levels"),
        ("gpt-5", "", "levels"),
        ("claude-sonnet-4-5", "", "budget"),
        ("claude-3-5-haiku", "", "none"),
        ("gemma3:12b", "gemma3", "none"),
        ("llama3.1:8b", "llama", "none"),
    ],
)
def test_family_is_found_from_the_hint_or_the_name(name, family, kind):
    assert cap(name, family).kind == kind


def test_a_family_hint_beats_the_name():
    assert effort.capability("qwen3", "my-llama-finetune").kind == "toggle"


def test_levels_are_parsed_case_insensitively_and_unknown_ones_are_none():
    assert (effort.parse_level(" HIGH "), effort.parse_level("extreme")) == ("high", None)


def test_qwen3_on_ollama_uses_think_true_and_false():
    q = cap("qwen3:8b")
    assert effort.request_fields("high", q, "ollama").fields == {"think": True}
    assert effort.request_fields("off", q, "ollama").fields["think"] is False


def test_qwen3_off_on_ollama_also_sets_reasoning_effort_none():
    """Regression: Ollama's `/v1` route ignores `think`, so `off` kept thinking and a 120-token
    classifier answer came back empty."""
    got = effort.request_fields("off", cap("qwen3.5:4b"), "ollama")
    assert got.fields == {"think": False, "reasoning_effort": "none"}


def test_qwen3_on_an_openai_compatible_server_uses_template_kwargs():
    got = effort.request_fields("medium", cap("qwen3:8b"), "openai")
    assert got.fields == {"chat_template_kwargs": {"enable_thinking": True}}


def test_qwen3_on_llamacpp_sets_kwargs_and_a_budget_per_level():
    q = cap("qwen3:8b")
    assert effort.request_fields("low", q, "llamacpp").fields["reasoning_budget"] == 1024
    assert effort.request_fields("max", q, "llamacpp").fields["reasoning_budget"] == -1
    off = effort.request_fields("off", q, "llamacpp").fields
    assert off["reasoning_budget"] == 0 and off["chat_template_kwargs"] == {"enable_thinking": False}


def test_gpt_oss_maps_max_to_high_and_off_to_the_lowest_level_with_a_note():
    g = cap("gpt-oss:20b")
    assert effort.request_fields("max", g, "openai").fields == {"reasoning_effort": "high"}
    off = effort.request_fields("off", g, "openai")
    assert off.fields == {"reasoning_effort": "low"} and "lowest level" in off.note


def test_gpt_oss_on_ollama_sets_think_and_reasoning_effort():
    assert effort.request_fields("medium", cap("gpt-oss:20b"), "ollama").fields == {
        "think": "medium",
        "reasoning_effort": "medium",
    }


def test_openai_cloud_reasoning_model_gets_reasoning_effort():
    assert effort.request_fields("high", cap("o3"), "openai-cloud").fields == {"reasoning_effort": "high"}


def test_anthropic_gets_a_token_budget_per_level():
    c = cap("claude-sonnet-4-5")
    budgets = [
        effort.request_fields(lv, c, "anthropic").fields["thinking"]["budget_tokens"]
        for lv in ("low", "medium", "high")
    ]
    assert budgets == [1024, 4096, 12288]
    assert min(effort.ANTHROPIC_BUDGETS.values()) >= 1024  # the API minimum


def test_anthropic_off_disables_thinking():
    got = effort.request_fields("off", cap("claude-sonnet-4-5"), "anthropic")
    assert got.fields == {"thinking": {"type": "disabled"}}


def test_deepseek_r1_ignores_effort_and_says_off_cannot_be_honoured():
    got = effort.request_fields("off", cap("deepseek-r1:14b"), "ollama")
    assert got.fields == {} and not got.applied and "cannot be honoured" in got.note


def test_a_model_that_cannot_think_gets_no_fields_and_a_note():
    got = effort.request_fields("high", cap("llama3.1:8b"), "ollama")
    assert got.fields == {} and "does not think" in got.note


def test_unknown_level_and_backend_are_errors():
    with pytest.raises(ValueError, match="effort level"):
        effort.request_fields("extreme", cap("qwen3:8b"), "ollama")
    with pytest.raises(ValueError, match="backend kind"):
        effort.request_fields("low", cap("qwen3:8b"), "vllm")

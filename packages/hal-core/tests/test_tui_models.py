"""Tests for `/model` by source, `/model save`, `/effort`, and the effort sent to the agent, on fakes only."""

import asyncio
import io

import pytest
from test_tui import NullClient

from halcore.tui import choices, slash
from halcore.tui.render import Renderer
from halkit import discovery, modelconf
from halkit.modelinfo import ModelInfo

SOURCES = {
    "local": modelconf.Source("local", "ollama", url="http://127.0.0.1:11434"),
    "lms": modelconf.Source("lms", "openai", url="http://127.0.0.1:1234/v1"),
    "weights": modelconf.Source("weights", "gguf", path="/models"),
}
MODELS = {
    "local": [ModelInfo("local", "gemma3:12b", "ollama", size_bytes=7 * 1024**3, family="gemma3"),
              ModelInfo("local", "qwen3:8b", "ollama", size_bytes=5 * 1024**3, family="qwen3")],
    "lms": [ModelInfo("lms", "phi-4", "openai", family="phi")],
    "weights": [ModelInfo("weights", "coder.gguf", "gguf", path="/models/coder.gguf"),
                ModelInfo("weights", "held.gguf", "gguf", path="/models/held.gguf")],
}
ROLES = {"chat": modelconf.Role("chat", "weights", "held.gguf")}


def discover(src):
    return discovery.Discovery(src.name, tuple(MODELS[src.name]))


def make_state(model="gemma3:12b", saved=None):
    st = slash.TuiState(NullClient(), "0123456789ab", "/work", Renderer(io.StringIO()), model=model)
    st.status.model = model
    st.load_choices = lambda: choices.load(SOURCES, ROLES, "http://127.0.0.1:11434", discover)
    st.save_model = lambda c, level: (saved.append((c.source, c.name, level)) if saved is not None else None) or ["wrote config"]
    return st


def run(line, st):
    return asyncio.run(slash.dispatch(line, st, {}))


def test_model_lists_discovered_models_grouped_by_source_with_the_current_one_marked():
    text = run("/model", make_state()).message
    lines = text.splitlines()
    assert lines.index("local (ollama)") < lines.index("lms (openai)") < lines.index("weights (gguf)")
    assert any(line.startswith("*1  gemma3:12b") for line in lines) and any(line.startswith(" 2  qwen3:8b") for line in lines)
    assert "[not served yet" in text and "thinks" in text


def test_selecting_a_model_switches_the_session_to_its_gateway_name():
    st = make_state()
    assert "model: qwen3:8b" in run("/model 2", st).message
    run("/model lms/phi-4", st)
    run("/model held", st)  # a GGUF model that a role holds is served by the router preset
    assert st.client.models == ["qwen3:8b", "lms/phi-4", "router/tuned-chat"] and st.status.model == "router/tuned-chat"


def test_a_model_the_gateway_cannot_serve_yet_is_refused_with_the_reason():
    st = make_state()
    msg = run("/model coder.gguf", st).message
    assert "cannot be used yet" in msg and st.client.models == []


def test_an_unknown_name_is_passed_to_the_gateway_as_it_is():
    st = make_state()
    run("/model fast", st)
    assert st.client.models == ["fast"]


def test_model_save_writes_the_current_model_as_the_code_role_with_the_effort():
    saved = []
    st = make_state("qwen3:8b", saved)
    run("/effort high", st)
    msg = run("/model save", st).message
    assert saved == [("local", "qwen3:8b", "high")] and "as the code role" in msg and "wrote config" in msg


def test_model_save_refuses_a_model_that_is_not_in_a_source_and_survives_a_write_error():
    assert "not one of your sources" in run("/model save", make_state("mystery")).message

    def boom(c, level):
        raise ValueError("disk full")

    st = make_state("qwen3:8b")
    st.save_model = boom
    assert "disk full" in run("/model save", st).message


def test_effort_without_an_argument_names_the_level_and_the_supported_levels():
    st = make_state("qwen3:8b")
    assert "default; this model supports off, low, medium, high, max" in run("/effort", st).message
    run("/effort low", st)
    assert st.client.efforts == ["low"] and st.effort == st.status.effort == "low"
    assert run("/effort", st).message.startswith("effort: low")


def test_effort_is_refused_with_a_reason_for_a_model_that_cannot_think():
    st = make_state("gemma3:12b")
    msg = run("/effort high", st).message
    assert "not available" in msg and "no thinking control" in msg and st.client.efforts == [] and st.effort == ""
    assert "no thinking control" in run("/effort", st).message


def test_a_bad_level_is_refused_and_default_clears_the_level():
    st = make_state("qwen3:8b")
    assert "use one of" in run("/effort loud", st).message
    run("/effort max", st)
    run("/effort default", st)
    assert st.client.efforts == ["max", ""] and st.effort == ""


def test_switching_to_a_model_without_support_drops_the_effort_and_says_so():
    st = make_state("qwen3:8b")
    run("/effort high", st)
    msg = run("/model gemma3:12b", st).message
    assert "effort high dropped" in msg and st.effort == "" and st.client.efforts == ["high", ""]


def test_the_code_role_effort_is_applied_at_start_only_when_the_model_supports_it():
    st = make_state("qwen3:8b")
    asyncio.run(slash.apply_code_defaults(st, {"code": modelconf.Role("code", "local", "qwen3:8b", effort="medium")}))
    assert st.client.efforts == ["medium"] and st.status.effort == "medium"
    other = make_state("gemma3:12b")
    asyncio.run(slash.apply_code_defaults(other, {"code": modelconf.Role("code", "local", "gemma3:12b", effort="medium")}))
    assert other.client.efforts == []


def test_clear_resends_the_effort_to_the_new_session():
    st = make_state("qwen3:8b")
    run("/effort high", st)

    async def fresh():
        return "ffffffffffff"

    st.new_conversation = fresh
    run("/clear", st)
    assert st.client.efforts == ["high", "high"] and st.sid == "ffffffffffff"


def test_help_lists_effort_and_every_builtin_has_a_help_row():
    assert "effort" in slash.COMMAND_HELP and set(slash.BUILTINS) - {"apply"} <= set(slash.COMMAND_HELP)


def test_a_source_that_is_not_the_hal_ollama_url_needs_the_saved_backend():
    other = {"a": modelconf.Source("a", "ollama", url="http://127.0.0.1:9999")}
    name, note = choices.gateway_name(other["a"], "m", {}, "http://127.0.0.1:11434")
    assert name == "a/m" and "/model save" in note


@pytest.mark.parametrize("text,expect", [("gemma", "gemma3:12b"), ("local/qwen3:8b", "qwen3:8b"), ("nothing", None)])
def test_find_matches_names_sources_and_unique_substrings(text, expect):
    found = choices.find(choices.load(SOURCES, ROLES, "http://127.0.0.1:11434", discover), text)
    assert (found.name if found else None) == expect

"""Tests for `halagent.profiles`: shipped catalogue, resolution order and strict validation."""

import pytest

from halagent.profiles import SHIPPED, ProfileError, load_catalogue, resolve_profile


def write(tmp_path, text):
    path = tmp_path / "model-profiles.toml"
    path.write_text(text)
    return path


def test_shipped_catalogue_is_valid():
    defaults, entries = load_catalogue(SHIPPED)
    assert defaults["context"] == 32768 and {e["family"] for e in entries} >= {"gemma4", "qwen35"}


def test_glob_match_is_case_insensitive_and_sets_parsers():
    p = resolve_profile("Gemma4:12B")
    assert p.family == "gemma4" and p.values["leak_parsers"][0] == "gemma4"
    assert p.sources["leak_parsers"] == "shipped:gemma4" and p.sources["context"] == "defaults"


def test_unknown_model_gets_the_defaults():
    p = resolve_profile("some-model")
    assert p.family == "default" and p.values["tool_format"] == "native"


def test_hal_profile_selects_by_family_before_globs():
    assert resolve_profile("hal-code", hal_profile="qwen35").family == "qwen35"


def test_resolution_order_is_user_then_overrides_then_cli(tmp_path):
    user = write(
        tmp_path, 'schema = 1\n[[model]]\nmatch = ["gemma4:*"]\nfamily = "mine"\ntemperature = 0.2\n'
    )
    p = resolve_profile("gemma4:12b", user_file=user)
    assert p.values["temperature"] == 0.2 and p.sources["temperature"] == "user:mine"
    p = resolve_profile("gemma4:12b", user_file=user, overrides={"temperature": 0.3}, cli={"top_k": 5})
    assert p.values["temperature"] == 0.3 and p.sources["top_k"] == "cli"


@pytest.mark.parametrize(
    "body, message",
    [
        ("schema = 2\n", "schema must be 1"),
        ("schema = 1\n[defaults]\ntemprature = 1\n", "unknown key 'temprature'"),
        ("schema = 1\n[defaults]\ncontext = 'big'\n", "context must be int"),
        ("schema = 1\n[defaults]\nleak_parsers = ['nope']\n", "unknown parser"),
        ("schema = 1\n[[model]]\nfamily = 'x'\n", "match must be"),
        ("schema = 1\n[defaults]\ntool_format = 'xml'\n", "must be one of"),
    ],
)
def test_invalid_file_fails_loudly_with_file_and_key(tmp_path, body, message):
    with pytest.raises(ProfileError, match=message) as exc:
        resolve_profile("m", user_file=write(tmp_path, body))
    assert "model-profiles.toml" in str(exc.value)


def test_request_extras_hold_only_sampling_and_cache_keys():
    extras = resolve_profile("gemma4:12b").request_extras()
    assert extras == {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "cache_prompt": True}


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("qwen2.5-coder:32b", "qwen25coder"),
        ("devstral-small-2:24b", "devstral"),
        ("Devstral-Small-2507-Q4_K_M", "devstral"),
        ("glm-4.6:9b", "glm47"),
        ("deepseek-r1:14b", "deepseek"),
        ("llama3.3:70b", "llama3"),
        ("gpt-oss:20b", "gptoss"),
        ("qwen3-coder:30b", "qwen3coder"),
    ],
)
def test_common_local_families_resolve_to_their_profiles(model, family):
    assert resolve_profile(model).family == family

"""Tests for `halkit.modelconf`: sources, roles and their validation."""

import pytest

from halkit import config, modelconf


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


FULL = """
[models]
context_cap = 65536
[models.sources.local]
kind = "ollama"
url = "http://127.0.0.1:11434"
[models.sources.lms]
kind = "openai"
url = "http://127.0.0.1:1234/v1"
api_key_file = "~/.config/hal/lms.key"
[models.sources.weights]
kind = "gguf"
path = "~/models"
[models.sources.claude]
kind = "anthropic"
api_key_file = "~/.config/hal/claude.key"
[models.roles.chat]
source = "local"
model = "gemma3:12b"
effort = "medium"
temperature = 0.7
"""


def test_a_full_config_loads_sources_and_roles(tmp_path):
    path = write(tmp_path, FULL)
    sources = modelconf.load_sources(path)
    assert {n: s.kind for n, s in sources.items()} == {
        "local": "ollama",
        "lms": "openai",
        "weights": "gguf",
        "claude": "anthropic",
    }
    role = modelconf.load_roles(path)["chat"]
    assert (role.model, role.effort, role.temperature, role.auto, role.gpu_layers) == (
        "gemma3:12b",
        "medium",
        0.7,
        True,
        -1,
    )
    assert modelconf.limits(path)["context_cap"] == 65536


def test_no_models_table_gives_no_sources_and_the_default_cap(tmp_path):
    path = write(tmp_path, "")
    assert modelconf.load_sources(path) == {}
    assert modelconf.limits(path)["context_cap"] == modelconf.DEFAULT_CONTEXT_CAP


@pytest.mark.parametrize("name", ["api_key", "key", "token"])
def test_a_key_stored_inline_is_refused_with_advice(tmp_path, name):
    path = write(tmp_path, f'[models.sources.x]\nkind = "openai-cloud"\n{name} = "sk-123"\n')
    with pytest.raises(config.ConfigError, match="never stored in the config"):
        modelconf.load_sources(path)


def test_an_unknown_source_key_is_refused(tmp_path):
    path = write(tmp_path, '[models.sources.x]\nkind = "gguf"\npath = "/m"\ncolour = "red"\n')
    with pytest.raises(config.ConfigError, match="no key 'colour'"):
        modelconf.load_sources(path)


def test_a_remote_http_url_is_refused_without_allow_remote(tmp_path):
    path = write(tmp_path, '[models.sources.x]\nkind = "openai"\nurl = "http://192.168.1.5:8000/v1"\n')
    with pytest.raises(config.ConfigError, match="not loopback"):
        modelconf.load_sources(path)


def test_a_remote_https_url_is_accepted_with_allow_remote(tmp_path):
    path = write(
        tmp_path,
        '[models.sources.x]\nkind = "openai"\nurl = "https://gpu.example.org/v1"\nallow_remote = true\n',
    )
    assert modelconf.load_sources(path)["x"].url == "https://gpu.example.org/v1"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('kind = "gguf"', "needs path"),
        ('kind = "anthropic"', "needs api_key_file"),
        ('kind = "ollama"\npath = "/m"', "does not apply"),
        ('kind = "carrier-pigeon"', "not one of"),
        ("", "needs a kind"),
    ],
)
def test_a_source_missing_what_its_kind_needs_is_refused(tmp_path, body, message):
    path = write(tmp_path, f"[models.sources.x]\n{body}\n")
    with pytest.raises(config.ConfigError, match=message):
        modelconf.load_sources(path)


def test_ollama_and_openai_sources_default_their_url(tmp_path):
    path = write(tmp_path, '[models.sources.a]\nkind = "ollama"\n')
    assert modelconf.load_sources(path)["a"].url == "http://127.0.0.1:11434"


def test_a_role_must_use_a_known_role_name_and_source(tmp_path):
    path = write(tmp_path, FULL + '[models.roles.poet]\nsource = "local"\nmodel = "m"\n')
    with pytest.raises(config.ConfigError, match="not a role"):
        modelconf.load_roles(path)
    path = write(tmp_path, FULL + '[models.roles.fast]\nsource = "ghost"\nmodel = "m"\n')
    with pytest.raises(config.ConfigError, match="not in \\[models.sources\\]"):
        modelconf.load_roles(path)


@pytest.mark.parametrize(
    "line", ['kv_type = "q5"', 'effort = "extreme"', "temperature = 3.5", "gpu_layers = -2", "context = -5"]
)
def test_bad_role_values_are_refused(tmp_path, line):
    path = write(tmp_path, FULL + f'[models.roles.fast]\nsource = "local"\nmodel = "m"\n{line}\n')
    with pytest.raises(config.ConfigError):
        modelconf.load_roles(path)

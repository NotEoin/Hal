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


def test_no_models_table_gives_no_sources_and_no_context_cap(tmp_path):
    # auto-tune aims for each model's own maximum unless the user sets a cap
    path = write(tmp_path, "")
    assert modelconf.load_sources(path) == {}
    assert modelconf.limits(path)["context_cap"] == modelconf.NO_CONTEXT_CAP == 0


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


def test_a_role_must_use_a_valid_role_name_and_a_known_source(tmp_path):
    path = write(tmp_path, FULL + '[models.roles.Poet_1]\nsource = "local"\nmodel = "m"\n')
    with pytest.raises(config.ConfigError, match="lowercase letters"):
        modelconf.load_roles(path)
    path = write(tmp_path, FULL + '[models.roles.guard]\nsource = "local"\nmodel = "m"\n')
    with pytest.raises(config.ConfigError, match="already a role name"):
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


def test_a_named_role_loads_and_is_listed_after_the_built_in_ones(tmp_path):
    role = '[models.roles.code-128k]\nsource = "local"\nmodel = "m"\ncontext = 131072\nauto = false\n'
    path = write(tmp_path, FULL + role)
    roles = modelconf.load_roles(path)
    assert roles["code-128k"].context == 131072 and modelconf.is_custom("code-128k")
    assert modelconf.role_names(roles) == [*modelconf.ROLES, "code-128k"]


def test_an_old_fast_role_table_loads_as_the_classifier(tmp_path):
    # 0.2.0 renamed the fast role (hal-fast) to classifier (hal-classifier)
    path = tmp_path / "config.toml"
    old = '[models.roles.fast]\nsource = "l"\nmodel = "g:8b"\n'
    path.write_text('[models.sources.l]\nkind = "ollama"\n' + old)
    roles = modelconf.load_roles(path)
    assert list(roles) == ["classifier"] and roles["classifier"].model == "g:8b"

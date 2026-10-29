"""Tests for `halkit.config`."""

from pathlib import Path

import pytest

from halkit import config
from halkit.config import ConfigError, Key, Table

DEMO = Table(
    name="demo",
    owner="hal-demo",
    keys=(
        Key("name", str, "default-name", env="DEMO_NAME"),
        Key("count", int, 3, env="DEMO_COUNT"),
        Key("ratio", float, 0.5),
        Key("flag", bool, False, env="DEMO_FLAG"),
        Key("items", list, [], env="DEMO_ITEMS"),
        Key("mode", str, "a", choices=("a", "b")),
        Key("port", int, 80, check=lambda v: None if 0 < v < 65536 else "port out of range"),
        Key("new_name", str, "x", deprecated_alias="old_name"),
    ),
)
NESTED = Table(name="agent.budgets", owner="hal-agent", keys=(Key("turns", int, 10),))


@pytest.fixture
def write(tmp_path):
    def _write(text: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(text)
        return path

    return _write


@pytest.fixture(autouse=True)
def reset_warnings(monkeypatch):
    monkeypatch.setattr(config, "_warned_aliases", set())


def test_missing_file_gives_all_defaults(tmp_path):
    got = config.load(DEMO, path=tmp_path / "none.toml", environ={})
    assert got["name"] == "default-name" and got["count"] == 3


def test_malformed_toml_raises_with_file_and_line(write):
    path = write('[demo]\nname = "ok"\ncount = = 2\n')
    with pytest.raises(ConfigError) as err:
        config.load(DEMO, path=path, environ={})
    assert err.value.path == path and err.value.line == 3
    assert f"{path}:3" in str(err.value)


def test_unknown_key_raises_naming_file_table_and_key(write):
    path = write("[demo]\nnmae = 1\n")
    with pytest.raises(ConfigError) as err:
        config.load(DEMO, path=path, environ={})
    assert (err.value.path, err.value.table, err.value.key) == (path, "demo", "nmae")
    assert "hal-demo" in str(err.value)


def test_wrong_type_raises_naming_the_key(write):
    with pytest.raises(ConfigError, match=r"count \(file\): expected int, got str"):
        config.load(DEMO, path=write('[demo]\ncount = "three"\n'), environ={})


def test_bool_is_not_accepted_for_an_int_key(write):
    with pytest.raises(ConfigError, match="expected int"):
        config.load(DEMO, path=write("[demo]\ncount = true\n"), environ={})


def test_value_outside_choices_raises(write):
    with pytest.raises(ConfigError, match=r"not one of \['a', 'b'\]"):
        config.load(DEMO, path=write('[demo]\nmode = "c"\n'), environ={})


def test_check_message_is_reported(write):
    with pytest.raises(ConfigError, match="port out of range"):
        config.load(DEMO, path=write("[demo]\nport = 70000\n"), environ={})


def test_unknown_top_level_table_is_ignored(write):
    got = config.load(DEMO, path=write("[other]\nanything = 1\n"), environ={})
    assert got["name"] == "default-name"


def test_integer_is_accepted_for_a_float_key_and_converted(write):
    got = config.load(DEMO, path=write("[demo]\nratio = 2\n"), environ={})
    assert got["ratio"] == 2.0 and isinstance(got["ratio"], float)


def test_default_applies_at_the_lowest_level(tmp_path):
    assert config.load(DEMO, path=tmp_path / "x", environ={})["name"] == "default-name"


def test_file_beats_default(write):
    assert config.load(DEMO, path=write('[demo]\nname = "file"\n'), environ={})["name"] == "file"


def test_environment_beats_file(write):
    got = config.load(DEMO, path=write('[demo]\nname = "file"\n'), environ={"DEMO_NAME": "env"})
    assert got["name"] == "env"


def test_cli_beats_environment(write):
    got = config.load(DEMO, path=write(""), environ={"DEMO_NAME": "env"}, cli={"name": "cli"})
    assert got["name"] == "cli"


def test_environment_values_are_parsed_by_type(write):
    env = {"DEMO_COUNT": "7", "DEMO_FLAG": "Yes", "DEMO_ITEMS": '["a", "b"]'}
    got = config.load(DEMO, path=write(""), environ=env)
    assert (got["count"], got["flag"], got["items"]) == (7, True, ["a", "b"])


@pytest.mark.parametrize(("text", "expected"), [("1", True), ("true", True), ("0", False), ("no", False)])
def test_environment_bool_spellings(write, text, expected):
    assert config.load(DEMO, path=write(""), environ={"DEMO_FLAG": text})["flag"] is expected


def test_environment_value_of_the_wrong_type_names_the_variable(write):
    with pytest.raises(ConfigError, match="environment DEMO_COUNT"):
        config.load(DEMO, path=write(""), environ={"DEMO_COUNT": "many"})


def test_empty_environment_value_counts_as_unset(write):
    assert config.load(DEMO, path=write(""), environ={"DEMO_NAME": ""})["name"] == "default-name"


def test_cli_with_an_unknown_key_raises(write):
    with pytest.raises(ConfigError, match="no key 'nope'"):
        config.load(DEMO, path=write(""), environ={}, cli={"nope": 1})


def test_load_reads_the_file_on_every_call(write):
    path = write('[demo]\nname = "one"\n')
    assert config.load(DEMO, path=path, environ={})["name"] == "one"
    path.write_text('[demo]\nname = "two"\n')
    assert config.load(DEMO, path=path, environ={})["name"] == "two"


def test_dotted_table_name_reads_a_nested_section(write):
    got = config.load(NESTED, path=write("[agent.budgets]\nturns = 4\n"), environ={})
    assert got["turns"] == 4


def test_section_that_is_not_a_table_raises(write):
    with pytest.raises(ConfigError, match=r"\[demo\] must be a table"):
        config.load(DEMO, path=write("demo = 1\n"), environ={})


def test_open_table_passes_unknown_keys_through(write):
    free = Table(name="servers", owner="hal-agent", keys=(), open=True)
    got = config.load(free, path=write('[servers]\nalpha = "x"\n'), environ={})
    assert got == {"alpha": "x"}


def test_deprecated_alias_is_accepted_with_one_warning(write, caplog):
    path = write('[demo]\nold_name = "legacy"\n')
    with caplog.at_level("WARNING", logger="halkit.config"):
        assert config.load(DEMO, path=path, environ={})["new_name"] == "legacy"
        config.load(DEMO, path=path, environ={})
    assert len(caplog.records) == 1 and "use new_name" in caplog.records[0].getMessage()


def test_new_key_wins_over_its_deprecated_alias(write):
    path = write('[demo]\nnew_name = "new"\nold_name = "old"\n')
    assert config.load(DEMO, path=path, environ={})["new_name"] == "new"


def test_register_rejects_a_second_owner_for_a_table():
    config.register(Table(name="owned", owner="one", keys=()))
    with pytest.raises(ValueError, match="already registered by one"):
        config.register(Table(name="owned", owner="two", keys=()))


def test_register_is_idempotent_and_known_tables_lists_it():
    table = Table(name="idem", owner="one", keys=())
    config.register(table)
    config.register(table)
    assert [t.name for t in config.known_tables()].count("idem") == 1


def test_hal_table_defaults_come_from_one_place(tmp_path, monkeypatch):
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "absent.toml"))
    got = config.hal()
    assert got["chat_model"] == "gemma4:12b-it-qat" and got["embed_on_cpu"] is True


def test_hal_reads_the_file_named_by_hal_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[hal]\nuser_name = "Ada"\n')
    monkeypatch.setenv("HAL_CONFIG", str(path))
    assert config.hal()["user_name"] == "Ada"


def test_hal_table_rejects_a_non_loopback_ollama_url(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[hal]\nollama_url = "http://10.0.0.5:11434"\n')
    with pytest.raises(ConfigError, match="not loopback"):
        config.load(config.HAL_TABLE, path=path, environ={})


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.2:1", "http://[::1]:11434"])
def test_hal_table_accepts_loopback_ollama_urls(tmp_path, url):
    path = tmp_path / "c.toml"
    path.write_text(f'[hal]\nollama_url = "{url}"\n')
    assert config.load(config.HAL_TABLE, path=path, environ={})["ollama_url"] == url

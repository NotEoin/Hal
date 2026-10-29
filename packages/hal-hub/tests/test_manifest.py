"""Tests for `halhub.manifest`: v2 accepted and rejected key by key, v1 loaded fail-closed.

REQ-HUB-01 (strict v2), REQ-HUB-02 (v1 as `act`), REQ-HUB-03 (tool names), REQ-HUB-04
(parameter types), REQ-HUB-05 (reserved parameter names), REQ-HUB-60 (units), REQ-HUB-63
(pack, secrets, domains), REQ-HUB-64 (routine tools).
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from halhub.manifest import (
    ManifestDefaults,
    ManifestError,
    load_manifest,
    parse_requirement,
    version_satisfies,
)

HEADER = """
schema = 2
[module]
name = "demo"
summary = "test module"
python = "haldemo.tools"
"""

V1_MODULE = """
[module]
name = "demo"
summary = "s"
python = "m"
[[tools]]
name = "demo_x"
description = "d"
"""

TOOL = """
[[tools]]
name = "demo_echo"
description = "Echo."
tier = "read"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "hal-module.toml"
    path.write_text(textwrap.dedent(text))
    return path


def load(tmp_path: Path, text: str, **kwargs):
    return load_manifest(write(tmp_path, text), source=kwargs.pop("source", "path"), **kwargs)


def rejects(tmp_path: Path, text: str, key: str | None, fragment: str = "") -> ManifestError:
    with pytest.raises(ManifestError) as info:
        load(tmp_path, text)
    assert info.value.key == key
    assert fragment in str(info.value)
    assert str(tmp_path / "hal-module.toml") in str(info.value)
    return info.value


# ---- the fixtures load ------------------------------------------------------------------------


def test_demo_fixture_loads_every_tool_with_its_flags(fixtures):
    spec = load_manifest(fixtures / "demo" / "hal-module.toml", source="path")
    tools = {t.name: t for t in spec.tools}
    assert spec.schema == 2 and spec.pack == "demo" and spec.version == "0"
    assert tools["demo_lights"].reversible and tools["demo_lights"].tier == "act"
    assert tools["demo_fetch"].untrusted_output and tools["demo_fetch"].network
    assert spec.secrets[0].name == "api_token" and spec.keywords == ("sample",)
    assert not spec.warnings


def test_v1_fixture_loads_with_warnings_and_conventional_units(fixtures):
    spec = load_manifest(fixtures / "legacy" / "hal-module.toml", source="path")
    assert spec.schema == 1 and spec.protocol == "oneshot"
    assert [u.file.name for u in spec.units] == ["hal-legacy.service"]
    assert all(u.file.is_absolute() for u in spec.units)
    assert [p.name for p in spec.desktop] == ["hal-legacy.desktop"]
    assert any("v1 manifest" in w for w in spec.warnings)


# ---- top level and [module] -------------------------------------------------------------------


def test_unknown_key_in_v2_manifest_names_file_and_key(tmp_path):
    error = rejects(tmp_path, HEADER + "colour = 'red'\n" + TOOL, "module.colour", "unknown key")
    assert error.path == tmp_path / "hal-module.toml"


def test_unknown_top_level_table_in_v2_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL + "[extra]\na = 1\n", "extra")


def test_schema_other_than_two_is_rejected(tmp_path):
    rejects(tmp_path, HEADER.replace("schema = 2", "schema = 3") + TOOL, "schema")
    rejects(tmp_path, HEADER.replace("schema = 2", "schema = true") + TOOL, "schema")


def test_malformed_toml_is_a_manifest_error(tmp_path):
    rejects(tmp_path, "schema = 2\n[module\n", None, "malformed TOML")


def test_missing_module_table_is_rejected(tmp_path):
    rejects(tmp_path, "schema = 2\n", "module", "required")


@pytest.mark.parametrize("name", ["Demo", "1demo", "de_mo", "de-mo", "a" * 33, "hal"])
def test_bad_module_name_is_rejected(tmp_path, name):
    rejects(tmp_path, HEADER.replace('"demo"', f'"{name}"', 1), "module.name")


def test_missing_summary_is_rejected(tmp_path):
    rejects(tmp_path, HEADER.replace('summary = "test module"\n', "") + TOOL, "module.summary", "required")


def test_long_summary_is_rejected(tmp_path):
    rejects(tmp_path, HEADER.replace("test module", "x" * 201) + TOOL, "module.summary")


def test_wrong_type_names_the_expected_type(tmp_path):
    rejects(tmp_path, HEADER + "inprocess = 'yes'\n" + TOOL, "module.inprocess", "expected boolean")


def test_module_with_tools_needs_python_or_exec(tmp_path):
    rejects(tmp_path, HEADER.replace('python = "haldemo.tools"\n', "") + TOOL, "module", "exactly one")
    both = HEADER + 'exec = ["prog"]\n' + TOOL
    rejects(tmp_path, both, "module", "exactly one")


def test_module_without_tools_needs_no_code(tmp_path):
    spec = load(tmp_path, HEADER.replace('python = "haldemo.tools"\n', ""))
    assert spec.tools == () and spec.python is None


def test_pack_defaults_to_module_name(tmp_path):
    assert load(tmp_path, HEADER + TOOL).pack == "demo"


def test_declared_pack_is_kept_for_the_registry_to_settle(tmp_path):
    spec = load(tmp_path, HEADER + 'pack = "organiser"\n' + TOOL)
    assert spec.pack == "organiser" and spec.declared_pack == "organiser"
    assert spec.tools[0].pack == "organiser"


def test_mcp_prefixed_pack_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + 'pack = "mcp-files"\n' + TOOL, "module.pack", "reserved")


def test_bad_pack_name_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + 'pack = "Organiser"\n' + TOOL, "module.pack")


def test_version_must_be_dotted_numbers(tmp_path):
    assert load(tmp_path, HEADER + 'version = "0.1.2"\n' + TOOL).version == "0.1.2"
    rejects(tmp_path, HEADER + 'version = "one"\n' + TOOL, "module.version")


def test_protocol_defaults_to_jsonl_for_v2_exec(tmp_path):
    text = HEADER.replace('python = "haldemo.tools"', 'exec = ["{root}/run.sh"]') + TOOL
    spec = load(tmp_path, text)
    assert spec.protocol == "jsonl" and spec.exec == (f"{tmp_path}/run.sh",)


def test_protocol_without_exec_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + 'protocol = "jsonl"\n' + TOOL, "module.protocol")


def test_placeholders_are_substituted(tmp_path, hal_home):
    argv = 'exec = ["{python}", "{root}/w.py", "{state}", "{data}"]'
    spec = load(tmp_path, HEADER.replace('python = "haldemo.tools"', argv) + TOOL)
    assert spec.exec[1] == f"{tmp_path}/w.py"
    assert spec.exec[2] == str(hal_home.state / "modules" / "demo")
    assert spec.exec[3] == str(hal_home.data / "modules" / "demo")


def test_unknown_placeholder_is_rejected(tmp_path):
    text = HEADER.replace('python = "haldemo.tools"', 'exec = ["{home}/w"]') + TOOL
    rejects(tmp_path, text, "module.exec[0]", "unknown placeholder")


def test_manifest_interpreter_counts_for_path_scanned_modules_only(tmp_path):
    text = HEADER + 'interpreter = ["{root}/.venv/bin/python"]\n' + TOOL
    assert load(tmp_path, text).interpreter == (f"{tmp_path}/.venv/bin/python",)
    assert load(tmp_path, text, source="entry_point").interpreter is None


def test_record_interpreter_wins_over_manifest_interpreter(tmp_path):
    text = HEADER + 'interpreter = ["/x/python"]\n' + TOOL
    spec = load(tmp_path, text, source="installed", interpreter=("/env/bin/python",))
    assert spec.interpreter == ("/env/bin/python",)


def test_interpreter_needs_a_python_module(tmp_path):
    text = HEADER.replace('python = "haldemo.tools"', 'exec = ["w"]') + 'interpreter = ["/p"]\n' + TOOL
    rejects(tmp_path, text, "module.interpreter")


def test_bad_requirement_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + 'requires = ["memory>>0.1"]\n' + TOOL, "module.requires[0]")


def test_requires_hal_must_be_a_specifier(tmp_path):
    assert load(tmp_path, HEADER + 'requires_hal = ">=0.1,<0.2"\n' + TOOL).requires_hal == ">=0.1,<0.2"
    rejects(tmp_path, HEADER + 'requires_hal = "~0.1"\n' + TOOL, "module.requires_hal")


def test_calls_must_name_tools(tmp_path):
    rejects(tmp_path, HEADER + 'calls = ["notify"]\n' + TOOL, "module.calls[0]")


def test_env_names_must_be_variable_names(tmp_path):
    rejects(tmp_path, HEADER + 'env = ["NOT OK"]\n' + TOOL, "module.env[0]")


@pytest.mark.parametrize("host", ["Example.org", "localhost", "-a.org", "b\\u00fccher.de", "a..org"])
def test_bad_domain_is_rejected(tmp_path, host):
    rejects(tmp_path, HEADER + f'domains = ["{host}"]\n' + TOOL, "module.domains[0]")


def test_wildcard_and_idna_domains_are_accepted(tmp_path):
    spec = load(tmp_path, HEADER + 'domains = ["*.example.org", "xn--bcher-kva.de"]\n' + TOOL)
    assert spec.domains == ("*.example.org", "xn--bcher-kva.de")


def test_duplicate_secret_names_are_rejected(tmp_path):
    secret = '[[module.secrets]]\nname = "pw"\nconfig_key = "demo.pw_file"\ndefault_path = "~/.pw"\n'
    rejects(tmp_path, HEADER + secret + secret + TOOL, "module.secrets[1].name", "duplicate")


def test_secret_needs_every_key(tmp_path):
    secret = '[[module.secrets]]\nname = "pw"\nconfig_key = "demo.pw_file"\n'
    rejects(tmp_path, HEADER + secret + TOOL, "module.secrets[0].default_path", "required")


def test_oneshot_exec_module_cannot_declare_secrets(tmp_path):
    secret = '[[module.secrets]]\nname = "pw"\nconfig_key = "demo.pw_file"\ndefault_path = "~/.pw"\n'
    text = HEADER.replace('python = "haldemo.tools"', 'exec = ["w"]\nprotocol = "oneshot"') + secret + TOOL
    rejects(tmp_path, text, "module.secrets", "jsonl")


def test_max_workers_is_bounded(tmp_path):
    rejects(tmp_path, HEADER + "max_workers = 9\n" + TOOL, "module.max_workers")


def test_keywords_accept_a_string_or_a_list(tmp_path):
    assert load(tmp_path, HEADER + 'keywords = "diary agenda"\n' + TOOL).keywords == ("diary", "agenda")
    assert load(tmp_path, HEADER + 'keywords = ["diary", "agenda"]\n' + TOOL).keywords == ("diary", "agenda")


# ---- [[tools]] ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["echo", "demo", "demo_", "demo_Echo", "other_echo", "demo_a-b"])
def test_tool_name_must_carry_the_module_prefix(tmp_path, name):
    rejects(tmp_path, HEADER + TOOL.replace("demo_echo", name), "tools[0].name")


def test_double_underscore_tool_name_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL.replace("demo_echo", "demo__echo"), "tools[0].name")


def test_tool_name_over_64_characters_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL.replace("demo_echo", "demo_" + "x" * 60), "tools[0].name")


def test_hal_prefix_belongs_to_the_hub_module(tmp_path):
    hub = HEADER.replace('name = "demo"', 'name = "hub"') + TOOL.replace("demo_echo", "hal_find")
    assert load(tmp_path, hub).tools[0].name == "hal_find"


def test_duplicate_tool_names_are_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL + TOOL, "tools[1].name", "duplicate")


def test_tier_is_required_in_v2(tmp_path):
    rejects(tmp_path, HEADER + TOOL.replace('tier = "read"\n', ""), "tools[0].tier", "required")


def test_unknown_tier_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL.replace('"read"', '"admin"'), "tools[0].tier")


def test_per_tool_default_verdict_key_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL + 'default = "allow"\n', "tools[0].default", "unknown key")


def test_confirm_is_not_a_v2_key(tmp_path):
    rejects(tmp_path, HEADER + TOOL + "confirm = false\n", "tools[0].confirm", "unknown key")


def test_timeout_above_cap_is_an_error_in_v2(tmp_path):
    rejects(tmp_path, HEADER + TOOL + "timeout = 7201\n", "tools[0].timeout")
    rejects(tmp_path, HEADER + TOOL + "timeout = 0.5\n", "tools[0].timeout")


def test_timeout_cap_comes_from_runner_config(tmp_path):
    with pytest.raises(ManifestError):
        load(tmp_path, HEADER + TOOL + "timeout = 100\n", defaults=ManifestDefaults(max_timeout=90))


def test_description_is_required_and_bounded(tmp_path):
    rejects(tmp_path, HEADER + TOOL.replace('description = "Echo."\n', ""), "tools[0].description")
    rejects(tmp_path, HEADER + TOOL.replace("Echo.", "x" * 2001), "tools[0].description")


def test_max_output_defaults_to_runner_config(tmp_path):
    spec = load(tmp_path, HEADER + TOOL, defaults=ManifestDefaults(max_result_chars=500))
    assert spec.tools[0].max_output == 500


ROUTINE = """
[[tools]]
name = "demo_save"
description = "Save a routine."
tier = "act"
routine = {id = "name", steps = "steps"}
[tools.params.name]
required = true
[tools.params.steps]
format = "json"
required = true
"""


def test_routine_tool_loads_with_its_parameters(tmp_path):
    tool = load(tmp_path, HEADER + ROUTINE).tools[0]
    assert tool.routine.id_param == "name" and tool.routine.steps_param == "steps"


def test_routine_tool_must_be_act_and_not_reviewable(tmp_path):
    rejects(tmp_path, HEADER + ROUTINE.replace('"act"', '"write"'), "tools[0].routine", "tier act")
    reviewable = ROUTINE.replace('tier = "act"', 'tier = "act"\nreviewable = true')
    rejects(tmp_path, HEADER + reviewable, "tools[0].routine", "reviewable")


def test_routine_steps_must_be_a_json_parameter(tmp_path):
    rejects(tmp_path, HEADER + ROUTINE.replace('format = "json"\n', ""), "tools[0].routine.steps")
    rejects(tmp_path, HEADER + ROUTINE.replace('id = "name"', 'id = "nope"'), "tools[0].routine.id")


# ---- [tools.params.<name>] ---------------------------------------------------------------------


def param(text: str) -> str:
    return HEADER + TOOL + "[tools.params.p]\n" + textwrap.dedent(text)


def test_param_named_confirmed_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL + "[tools.params.confirmed]\n", "tools[0].params.confirmed", "reserved")
    rejects(tmp_path, HEADER + TOOL + "[tools.params.confirm]\n", "tools[0].params.confirm", "reserved")


def test_underscore_param_name_is_rejected_in_v1_and_v2(tmp_path):
    rejects(tmp_path, HEADER + TOOL + "[tools.params._confirmed]\n", "tools[0].params._confirmed")
    v1 = V1_MODULE
    rejects(tmp_path, v1 + "[tools.params._x]\n", "tools[0].params._x")


def test_bad_param_name_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL + "[tools.params.Path]\n", "tools[0].params.Path")


@pytest.mark.parametrize("kind", ["array", "object", "float", "list"])
def test_v2_param_type_outside_the_four_is_rejected(tmp_path, kind):
    rejects(tmp_path, param(f'type = "{kind}"\n'), "tools[0].params.p.type")


def test_list_is_for_strings_only(tmp_path):
    rejects(tmp_path, param('type = "integer"\nlist = true\n'), "tools[0].params.p.list")


def test_item_type_needs_list(tmp_path):
    rejects(tmp_path, param('item_type = "integer"\n'), "tools[0].params.p.item_type")
    spec = load(tmp_path, param('list = true\nitem_type = "integer"\n'))
    assert spec.tools[0].params[0].item_type == "integer"


def test_enum_values_must_match_the_type(tmp_path):
    rejects(tmp_path, param('type = "integer"\nenum = [1, "two"]\n'), "tools[0].params.p.enum[1]")
    rejects(tmp_path, param("enum = []\n"), "tools[0].params.p.enum")


def test_default_must_match_the_type_in_v2(tmp_path):
    rejects(tmp_path, param('type = "integer"\ndefault = "3"\n'), "tools[0].params.p.default")


def test_bounds_only_for_numbers(tmp_path):
    rejects(tmp_path, param("minimum = 1\n"), "tools[0].params.p.minimum")
    rejects(tmp_path, param('type = "integer"\nminimum = 5\nmaximum = 1\n'), "tools[0].params.p.minimum")


def test_access_needs_path_format(tmp_path):
    rejects(tmp_path, param('access = "write"\n'), "tools[0].params.p.access")


def test_format_outside_the_set_is_rejected(tmp_path):
    rejects(tmp_path, param('format = "uri"\n'), "tools[0].params.p.format")


def test_format_is_for_strings_only(tmp_path):
    rejects(tmp_path, param('type = "integer"\nformat = "path"\n'), "tools[0].params.p.format")


def test_alias_may_not_shadow_another_parameter(tmp_path):
    text = HEADER + TOOL + '[tools.params.when_text]\naliases = ["when"]\n[tools.params.when]\n'
    rejects(tmp_path, text, "tools[0].params.when_text.aliases", "already")


def test_max_length_must_be_positive(tmp_path):
    rejects(tmp_path, param("max_length = 0\n"), "tools[0].params.p.max_length")


def test_unknown_param_key_is_rejected(tmp_path):
    rejects(tmp_path, param("requried = true\n"), "tools[0].params.p.requried", "unknown key")


# ---- [[install]], [[units]], [[desktop]] -------------------------------------------------------


def test_install_steps_are_parsed_with_placeholders(tmp_path):
    text = HEADER + TOOL + '[[install]]\nid = "fetch"\nsummary = "s"\nrun = ["{root}/get"]\nnetwork = true\n'
    step = load(tmp_path, text).install[0]
    assert step.run == (f"{tmp_path}/get",) and step.network and step.timeout == 600


def test_duplicate_install_ids_are_rejected(tmp_path):
    step = '[[install]]\nid = "a"\nsummary = "s"\nrun = ["x"]\n'
    rejects(tmp_path, HEADER + TOOL + step + step, "install[1].id", "duplicate")


def test_install_timeout_is_capped(tmp_path):
    step = '[[install]]\nid = "a"\nsummary = "s"\nrun = ["x"]\ntimeout = 9000\n'
    rejects(tmp_path, HEADER + TOOL + step, "install[0].timeout")


def test_declared_units_are_absolute_paths(tmp_path):
    units = '[[units]]\nfile = "systemd/a.service"\ngpu = true\n[[desktop]]\nfile = "d/a.desktop"\n'
    text = HEADER + TOOL + units
    spec = load(tmp_path, text)
    assert spec.units[0].file == tmp_path / "systemd" / "a.service" and spec.units[0].gpu
    assert spec.desktop == (tmp_path / "d" / "a.desktop",)


def test_unit_outside_the_module_or_with_a_wrong_suffix_is_rejected(tmp_path):
    rejects(tmp_path, HEADER + TOOL + '[[units]]\nfile = "../x.service"\n', "units[0].file")
    rejects(tmp_path, HEADER + TOOL + '[[units]]\nfile = "x.socket"\n', "units[0].file")


def test_v2_without_units_declares_none(tmp_path, fixtures):
    spec = load(tmp_path, HEADER + TOOL)
    assert spec.units == () and spec.desktop == ()


# ---- v1 compatibility --------------------------------------------------------------------------


def test_v1_array_param_becomes_comma_list(fixtures):
    tool = load_manifest(fixtures / "legacy" / "hal-module.toml", source="path").tools[1]
    tags = tool.param("tags")
    assert tags.type == "string" and tags.list and tags.item_type == "string"


def test_v1_object_param_becomes_json_format(fixtures):
    tool = load_manifest(fixtures / "legacy" / "hal-module.toml", source="path").tools[1]
    extra = tool.param("extra")
    assert extra.type == "string" and extra.format == "json" and extra.v1_object


def test_v1_timeout_above_cap_is_clamped_with_a_warning(fixtures):
    spec = load_manifest(fixtures / "legacy" / "hal-module.toml", source="path")
    assert spec.tools[1].timeout == 7200
    assert any("clamped" in w for w in spec.warnings)


def test_v1_unknown_keys_warn_instead_of_failing(tmp_path):
    v1 = '[module]\nname = "demo"\nsummary = "s"\npython = "m"\nflavour = 1\n'
    spec = load(tmp_path, v1 + '[[tools]]\nname = "demo_x"\ndescription = "d"\ntier = "read"\n')
    assert spec.tools[0].tier == "act"
    assert any("module.flavour" in w for w in spec.warnings)
    assert any("tools[0].tier" in w for w in spec.warnings)


def test_v1_exec_uses_oneshot_and_v1_install_becomes_one_step(tmp_path):
    v1 = '[module]\nname = "demo"\nsummary = "s"\nexec = ["{root}/run"]\ninstall = ["{root}/setup.sh"]\n'
    spec = load(tmp_path, v1 + '[[tools]]\nname = "demo_x"\ndescription = "d"\n')
    assert spec.protocol == "oneshot" and spec.exec == (f"{tmp_path}/run",)
    step = spec.install[0]
    assert (step.id, step.timeout, step.network) == ("setup", 3600, True)
    assert step.run == (f"{tmp_path}/setup.sh",)


def test_v1_manifest_logs_one_warning_naming_the_file(tmp_path, caplog):
    v1 = V1_MODULE
    with caplog.at_level(logging.WARNING, logger="halhub.manifest"):
        load(tmp_path, v1)
    v1_lines = [r for r in caplog.records if "v1 manifest" in r.getMessage()]
    assert len(v1_lines) == 1 and str(tmp_path / "hal-module.toml") in v1_lines[0].getMessage()


# ---- schema and helpers ------------------------------------------------------------------------


def test_input_schema_is_flat_and_closed(fixtures):
    spec = load_manifest(fixtures / "demo" / "hal-module.toml", source="path")
    schema = spec.tools[0].input_schema()
    assert schema["additionalProperties"] is False and schema["required"] == ["text"]
    assert schema["properties"]["times"] == {
        "type": "integer",
        "description": "how many copies",
        "default": 1,
        "minimum": 1,
        "maximum": 5,
    }
    assert schema["properties"]["text"]["maxLength"] == 200


def test_list_parameter_schema_says_comma_separated(fixtures):
    tool = load_manifest(fixtures / "legacy" / "hal-module.toml", source="path").tools[1]
    assert "comma-separated" in tool.input_schema()["properties"]["tags"]["description"]


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("0.1.0", ">=0.1,<0.2", True),
        ("0.2", ">=0.1,<0.2", False),
        ("0.1.0.dev0", ">=0.1", True),
        ("1.0", "!=1", False),
        ("1.2", "", True),
    ],
)
def test_version_specifiers_compare_dotted_numbers(version, spec, ok):
    assert version_satisfies(version, spec) is ok


def test_requirement_splits_name_and_clauses():
    assert parse_requirement("memory>=0.1") == ("memory", ">=0.1")
    assert parse_requirement("notify") == ("notify", "")

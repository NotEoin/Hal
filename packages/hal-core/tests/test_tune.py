"""Tests for `hal tune`, `hal models` and the engine behind them, on fakes only.

A fake Ollama on loopback, generated GGUF files and a hardware description file stand in for
the real machine; the config lives in a throwaway Hal home. No test reaches the real Ollama,
a GPU or the network.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from halcore import modelplan, modelscmd, tune
from halkit import hardware, modelconf
from halkit.testing import TempHalHome
from halkit.testing.gguf import llama_like, write_gguf
from halkit.testing.ollama import FakeOllama

MIB = 1024 * 1024
GIB = 1024 * MIB


def arch_doc(layers, heads, kv_heads, key_len, ctx, caps=("completion", "tools")):
    return {
        "capabilities": list(caps),
        "model_info": {
            "general.architecture": "llama",
            "llama.block_count": layers,
            "llama.attention.head_count": heads,
            "llama.attention.head_count_kv": kv_heads,
            "llama.attention.key_length": key_len,
            "llama.embedding_length": heads * key_len,
            "llama.context_length": ctx,
        },
    }


TAGS = [
    {"name": "big:70b", "size": 40 * GIB, "digest": "a" * 64, "details": {"family": "llama", "parameter_size": "70B"}},
    {"name": "gemma:12b", "size": 7000 * MIB, "digest": "b" * 64, "details": {"family": "llama", "parameter_size": "12B"}},
    {"name": "tiny:1b", "size": 800 * MIB, "digest": "c" * 64, "details": {"family": "llama", "parameter_size": "1B"}},
    {"name": "coder:7b", "size": 4500 * MIB, "digest": "e" * 64, "details": {"family": "llama", "parameter_size": "7B"}},
    {"name": "nomic-embed", "size": 270 * MIB, "digest": "d" * 64, "details": {"family": "bert"}},
]
SHOW = {
    "big:70b": arch_doc(80, 64, 8, 128, 131072),
    "gemma:12b": arch_doc(48, 32, 8, 128, 131072, ("completion", "tools", "vision")),
    "tiny:1b": arch_doc(16, 32, 8, 64, 131072),
    "coder:7b": arch_doc(32, 32, 8, 128, 32768),
    "nomic-embed": {"capabilities": ["embedding"], "model_info": {"general.architecture": "bert"}},
}
CARD_24GB = {
    "gpus": [{"index": 0, "vendor": "nvidia", "name": "RTX 4090", "total_mib": 24564, "free_mib": 24000, "compute_cap": "8.9"}],
    "ram_total_mib": 32768,
    "ram_free_mib": 24576,
    "cpu_threads": 16,
    "wsl2": False,
    "notes": [],
}
CPU_ONLY = {**CARD_24GB, "gpus": []}


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


@pytest.fixture
def ollama():
    with FakeOllama(TAGS, SHOW) as server:
        yield server


@pytest.fixture
def hw_file(tmp_path):
    def make(doc):
        path = tmp_path / "hw.json"
        path.write_text(json.dumps(doc))
        return str(path)

    return make


def run_json(capsys, *args):
    status = tune.main([*args, "--json"])
    return status, json.loads(capsys.readouterr().out)


def add_ollama(home, server):
    home.config.write_text(f'[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n')


def test_without_sources_tune_says_so_and_prints_the_next_step(home, hw_file, capsys):
    status = tune.main(["--auto", "--hardware", hw_file(CARD_24GB)])
    out = capsys.readouterr().out
    assert status == tune.EXIT_NOTHING
    assert "hal models add-source ollama http://127.0.0.1:11434" in out


def test_auto_json_on_a_24gb_card_picks_the_biggest_model_that_fits_for_chat(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    status, doc = run_json(capsys, "--auto", "--hardware", hw_file(CARD_24GB))
    roles = {r["role"]: r for r in doc["roles"]}
    assert status == 0 and doc["written"] is None
    chat = roles["chat"]
    assert chat["model"]["name"] == "gemma:12b"  # the 70B does not fit in VRAM; the 12B does, with context
    assert chat["plan"]["status"] == "gpu" and chat["plan"]["context"] >= 16384
    assert roles["fast"]["model"]["name"] == "tiny:1b"
    assert roles["embed"]["model"]["name"] == "nomic-embed"
    assert roles["code"]["model"]["name"] == "coder:7b"
    assert roles["vision"]["model"]["name"] == "gemma:12b"
    assert "writer" not in roles  # falls back to chat
    assert any("context" in line or "KV" in line for line in chat["plan"]["reasoning"])


def test_a_cpu_only_machine_gets_cpu_plans_and_a_model_that_is_small_enough(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    _, doc = run_json(capsys, "--auto", "--role", "chat", "--hardware", hw_file(CPU_ONLY))
    chat = doc["roles"][0]
    assert chat["plan"]["status"] == "cpu" and chat["plan"]["gpu_layers"] == 0
    assert chat["model"]["size_bytes"] <= modelplan.CPU_COMFORT_BYTES


def test_without_write_nothing_is_changed(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    before = home.config.read_text()
    tune.main(["--auto", "--hardware", hw_file(CARD_24GB)])
    assert home.config.read_text() == before
    assert not (home.config.parent / "models.toml").exists()


def test_write_saves_roles_pins_digests_and_names_the_models_for_the_gateway(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    status, doc = run_json(capsys, "--auto", "--write", "--hardware", hw_file(CARD_24GB))
    assert status == 0 and doc["written"]
    cfg = tomllib.loads(home.config.read_text())
    chat = cfg["models"]["roles"]["chat"]
    assert (chat["source"], chat["model"], chat["gpu_layers"]) == ("local", "gemma:12b", -1)
    assert chat["context"] >= 16384 and chat["kv_type"] in ("f16", "q8_0", "q4_0")
    assert cfg["hal"]["chat_model"] == "gemma:12b" and cfg["hal"]["fast_model"] == "tiny:1b"
    assert cfg["hal"]["ollama_url"] == ollama.url
    manifest = (home.config.parent / "models.toml").read_text()
    assert "gemma:12b@sha256:" + "b" * 64 in manifest
    assert len(list(home.config.parent.glob("config.toml.bak.*"))) == 1  # the file with only the source in it
    assert modelconf.load_roles(home.config)["chat"].model == "gemma:12b"


def test_a_second_write_keeps_a_backup_and_does_not_change_the_result(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    args = ["--auto", "--write", "--hardware", hw_file(CARD_24GB)]
    run_json(capsys, *args)
    first = home.config.read_text()
    run_json(capsys, *args)
    assert home.config.read_text() == first
    assert list(home.config.parent.glob("config.toml.bak.*"))


def test_an_assigned_role_is_kept_and_a_locked_role_keeps_its_numbers(home, ollama, hw_file, capsys):
    home.config.write_text(
        f'[models.sources.local]\nkind = "ollama"\nurl = "{ollama.url}"\n'
        '[models.roles.chat]\nsource = "local"\nmodel = "coder:7b"\ncontext = 4096\nkv_type = "q4_0"\n'
        "auto = false\neffort = \"low\"\n"
    )
    _, doc = run_json(capsys, "--auto", "--write", "--role", "chat", "--hardware", hw_file(CARD_24GB))
    assert doc["roles"][0]["assigned"] and doc["roles"][0]["locked"]
    chat = tomllib.loads(home.config.read_text())["models"]["roles"]["chat"]
    assert (chat["model"], chat["context"], chat["kv_type"], chat["effort"], chat["auto"]) == (
        "coder:7b", 4096, "q4_0", "low", False,
    )


def test_reassign_chooses_again_for_an_assigned_role(home, ollama, hw_file, capsys):
    home.config.write_text(
        f'[models.sources.local]\nkind = "ollama"\nurl = "{ollama.url}"\n'
        '[models.roles.chat]\nsource = "local"\nmodel = "tiny:1b"\n'
    )
    _, doc = run_json(capsys, "--auto", "--reassign", "--role", "chat", "--hardware", hw_file(CARD_24GB))
    assert doc["roles"][0]["model"]["name"] == "gemma:12b"


def test_an_assigned_model_that_is_gone_is_reported_not_replaced(home, ollama, hw_file, capsys):
    home.config.write_text(
        f'[models.sources.local]\nkind = "ollama"\nurl = "{ollama.url}"\n'
        '[models.roles.chat]\nsource = "local"\nmodel = "vanished:3b"\n'
    )
    _, doc = run_json(capsys, "--auto", "--role", "chat", "--hardware", hw_file(CARD_24GB))
    assert doc["roles"][0]["model"] is None and "vanished:3b not found" in doc["roles"][0]["notes"][0]


def test_cloud_and_remote_sources_are_never_chosen_automatically(home, tmp_path, hw_file, capsys):
    key = tmp_path / "k"
    key.write_text("x")
    key.chmod(0o600)
    home.config.write_text(f'[models.sources.cloud]\nkind = "openai-cloud"\napi_key_file = "{key}"\n')
    status, doc = run_json(capsys, "--auto", "--hardware", hw_file(CARD_24GB))
    assert all(r["model"] is None for r in doc["roles"])


def test_gguf_roles_get_a_generated_mode_a_router_backend_and_pinned_files(home, tmp_path, hw_file, capsys):
    models = tmp_path / "gguf"
    models.mkdir()
    write_gguf(models / "chatty-Q4_K_M.gguf", llama_like(), pad_to=3 * MIB)
    home.config.write_text(f'[models.sources.weights]\nkind = "gguf"\npath = "{models}"\n')
    status, doc = run_json(capsys, "--auto", "--write", "--role", "chat", "--hardware", hw_file(CARD_24GB))
    assert status == 0, doc
    cfg = tomllib.loads(home.config.read_text())
    assert cfg["hal"]["chat_model"] == "router/tuned-chat"
    backends = cfg["gateway"]["backends"]
    assert backends["router"]["type"] == "llama-router" and backends["router"]["mode"] == "tuned"
    assert backends["ollama"]["type"] == "ollama"
    mode = (home.config.parent / "modes" / "tuned.ini").read_text()
    assert "[tuned-chat]" in mode and f"model = {models / 'chatty-Q4_K_M.gguf'}" in mode
    assert "chatty-Q4_K_M" in (home.config.parent / "models.toml").read_text()


def test_an_anthropic_role_is_listed_but_left_out_of_the_gateway(home, tmp_path):
    key = tmp_path / "k"
    key.write_text("x")
    key.chmod(0o600)
    src = modelconf.Source("claude", "anthropic", api_key_file=str(key))
    from halkit.modelinfo import ModelInfo

    plan = modelplan.RolePlan("chat", src, ModelInfo("claude", "claude-sonnet-4-5", "anthropic"))
    wiring = modelplan.wire([plan], {})
    assert wiring.hal == {} and "no Anthropic backend" in wiring.notes[0]


def test_models_add_source_list_and_remove_round_trip(home, ollama, capsys):
    assert modelscmd.main(["add-source", "ollama", ollama.url]) == 0
    assert modelscmd.main(["add-source", "ollama", ollama.url]) == 0  # a second one gets the name ollama-2
    assert set(modelconf.load_sources(home.config)) == {"ollama", "ollama-2"}
    capsys.readouterr()
    assert modelscmd.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "gemma:12b" in out and "nomic-embed" in out
    assert modelscmd.main(["remove-source", "ollama-2"]) == 0
    assert set(modelconf.load_sources(home.config)) == {"ollama"}


def test_add_source_refuses_a_remote_url_and_a_missing_key_file(home, capsys):
    assert modelscmd.main(["add-source", "openai", "http://10.0.0.5:8000/v1"]) == 1
    assert modelscmd.main(["add-source", "anthropic"]) == 1
    assert "not loopback" not in capsys.readouterr().out
    assert not home.config.exists() or "10.0.0.5" not in home.config.read_text()


def test_remove_source_is_refused_while_a_role_uses_it(home, ollama, capsys):
    home.config.write_text(
        f'[models.sources.local]\nkind = "ollama"\nurl = "{ollama.url}"\n[models.roles.chat]\nsource = "local"\nmodel = "m"\n'
    )
    assert modelscmd.main(["remove-source", "local"]) == 1
    assert "use 'local'" in capsys.readouterr().err


def test_models_list_json_reports_an_unreachable_source(home, capsys):
    home.config.write_text('[models.sources.gone]\nkind = "ollama"\nurl = "http://127.0.0.1:9"\n')
    assert modelscmd.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["gone"]["error"]


def test_models_assign_sets_the_role_pins_it_and_wires_the_gateway(home, ollama, capsys, monkeypatch):
    monkeypatch.setattr(hardware, "default_runner", lambda argv, timeout: (_ for _ in ()).throw(FileNotFoundError()))
    add_ollama(home, ollama)
    assert modelscmd.main(["assign", "fast", "local", "tiny:1b"]) == 0
    cfg = tomllib.loads(home.config.read_text())
    assert cfg["models"]["roles"]["fast"]["model"] == "tiny:1b" and cfg["hal"]["fast_model"] == "tiny:1b"
    assert "tiny:1b@sha256:" + "c" * 64 in (home.config.parent / "models.toml").read_text()


def test_models_assign_refuses_a_model_the_source_does_not_have(home, ollama, capsys):
    add_ollama(home, ollama)
    assert modelscmd.main(["assign", "chat", "local", "nothing:1b"]) == 1
    assert "not available" in capsys.readouterr().err
    assert "models.roles" not in home.config.read_text()


def test_tune_and_models_are_registered_commands():
    from halcore import cli

    assert "tune" in cli.COMMANDS and "models" in cli.COMMANDS
    assert {"tune", "models"} <= {n for _, names in cli.GROUPS for n in names}


def test_a_bad_hardware_file_is_an_error_not_a_crash(home, tmp_path, capsys):
    bad = tmp_path / "hw.json"
    bad.write_text("{not json")
    assert tune.main(["--auto", "--hardware", str(bad)]) == tune.EXIT_ERROR
    assert "hal tune:" in capsys.readouterr().err


def test_the_text_plan_shows_hardware_numbers_and_the_write_hint(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    assert tune.main(["--auto", "--role", "chat", "--hardware", hw_file(CARD_24GB)]) == 0
    out = capsys.readouterr().out
    assert "RTX 4090 24564 MiB (24000 free)" in out and "local/gemma:12b" in out
    assert "budget" in out and "Run again with --write" in out


def test_a_failed_pin_rolls_back_earlier_pins_and_writes_no_config(home, ollama, tmp_path, hw_file, capsys, monkeypatch):
    # regression: `--write` pinned the Ollama models, then failed on a GGUF file and left them pinned
    models = tmp_path / "gguf"
    models.mkdir()
    write_gguf(models / "coder-Q4_K_M.gguf", llama_like(), pad_to=3 * MIB)
    home.config.write_text(
        f'[models.sources.local]\nkind = "ollama"\nurl = "{ollama.url}"\n[models.sources.weights]\nkind = "gguf"\npath = "{models}"\n'
    )
    before = home.config.read_text()

    def refuse(store, path, **kwargs):
        raise modelplan.ManifestError("disk full")

    monkeypatch.setattr(modelplan.gwmodels, "add_local", refuse)
    status = tune.main(["--auto", "--write", "--role", "chat", "--role", "code", "--hardware", hw_file(CARD_24GB)])
    err = capsys.readouterr().err
    assert status == tune.EXIT_ERROR
    assert "nothing was written" in err and "disk full" in err and "rolled back" in err
    assert home.config.read_text() == before
    assert not (home.config.parent / "models.toml").exists()
    assert not list(home.config.parent.glob("config.toml.bak.*"))


def no_gpu_probe(monkeypatch):
    monkeypatch.setattr(hardware, "default_runner", lambda argv, timeout: (_ for _ in ()).throw(FileNotFoundError()))


def test_assign_with_a_new_name_adds_a_named_role_served_as_a_gateway_alias(home, ollama, capsys, monkeypatch):
    from halgateway.config import load_config

    no_gpu_probe(monkeypatch)
    add_ollama(home, ollama)
    assert modelscmd.main(["assign", "code-big", "local", "coder:7b"]) == 0
    cfg = tomllib.loads(home.config.read_text())
    assert cfg["models"]["roles"]["code-big"]["model"] == "coder:7b"
    assert cfg["gateway"]["aliases"]["hal-code-big"] == {"backend": "local", "model": "coder:7b"}
    assert "code_model" not in cfg.get("hal", {})
    assert load_config(home.config).aliases["hal-code-big"]["backend"] == "local"


def test_remove_role_drops_the_named_role_and_its_alias_but_not_a_built_in_one(home, ollama, capsys, monkeypatch):
    no_gpu_probe(monkeypatch)
    add_ollama(home, ollama)
    assert modelscmd.main(["assign", "code-big", "local", "coder:7b"]) == 0
    assert modelscmd.main(["remove-role", "code-big"]) == 0
    text = home.config.read_text()
    assert "code-big" not in text
    assert modelscmd.main(["assign", "fast", "local", "tiny:1b"]) == 0
    assert modelscmd.main(["remove-role", "fast"]) == 1


def test_assign_refuses_a_role_name_that_cannot_be_an_alias(home, ollama, capsys):
    add_ollama(home, ollama)
    assert modelscmd.main(["assign", "Code 128k", "local", "coder:7b"]) == 1
    assert "lowercase letters" in capsys.readouterr().err


def test_a_named_gguf_role_gets_its_own_preset_in_the_tuned_mode(home, tmp_path, hw_file, capsys):
    models = tmp_path / "gguf"
    models.mkdir()
    write_gguf(models / "chatty-Q4_K_M.gguf", llama_like(), pad_to=3 * MIB)
    home.config.write_text(
        f'[models.sources.weights]\nkind = "gguf"\npath = "{models}"\n'
        '[models.roles.long-code]\nsource = "weights"\nmodel = "chatty-Q4_K_M"\n'
    )
    status, doc = run_json(capsys, "--write", "--role", "long-code", "--reassign", "--hardware", hw_file(CARD_24GB))
    assert status == 0, doc
    cfg = tomllib.loads(home.config.read_text())
    assert cfg["gateway"]["aliases"]["hal-long-code"] == {"backend": "router", "model": "tuned-long-code"}
    assert "[tuned-long-code]" in (home.config.parent / "modes" / "tuned.ini").read_text()


def test_tune_refuses_a_role_that_does_not_exist(home, ollama, hw_file, capsys):
    add_ollama(home, ollama)
    assert tune.main(["--role", "nope", "--hardware", hw_file(CARD_24GB)]) == tune.EXIT_ERROR
    assert "no role nope" in capsys.readouterr().err


def test_writing_one_gguf_role_keeps_the_presets_of_the_others_in_the_tuned_mode(home, tmp_path, hw_file, capsys):
    # regression: a write planned for one role rewrote tuned.ini with only that role's preset,
    # so the gateway lost every other GGUF role
    models = tmp_path / "gguf"
    models.mkdir()
    write_gguf(models / "chatty-Q4_K_M.gguf", llama_like(), pad_to=3 * MIB)
    home.config.write_text(f'[models.sources.weights]\nkind = "gguf"\npath = "{models}"\n')
    assert run_json(capsys, "--auto", "--write", "--role", "chat", "--hardware", hw_file(CARD_24GB))[0] == 0
    text = home.config.read_text() + '[models.roles.long-code]\nsource = "weights"\nmodel = "chatty-Q4_K_M"\n'
    home.config.write_text(text)
    assert run_json(capsys, "--write", "--role", "long-code", "--hardware", hw_file(CARD_24GB))[0] == 0
    mode = (home.config.parent / "modes" / "tuned.ini").read_text()
    assert "[tuned-chat]" in mode and "[tuned-long-code]" in mode

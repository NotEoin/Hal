"""Tests for `halgateway.tunedmode`: the generated mode renders and passes the mode checks."""

import pytest

from halgateway import modes, tunedmode
from halgateway.models import Store, add_local
from halgateway.models.manifest import Verifier, load_manifest
from halkit.testing.gguf import llama_like, write_gguf
from halkit.tuning import ALL_LAYERS


def preset(tmp_path, role, **kw):
    path = write_gguf(tmp_path / f"{role}.gguf", llama_like())
    base = dict(role=role, model_path=path, context=32768, kv_type="q8_0", gpu_layers=ALL_LAYERS)
    return tunedmode.Preset(**{**base, **kw})


def test_render_names_a_preset_per_role_and_makes_chat_the_main(tmp_path):
    text = tunedmode.render([preset(tmp_path, "fast"), preset(tmp_path, "chat")])
    assert "main = tuned-chat" in text and "[tuned-fast]" in text and "[tuned-chat]" in text
    assert "ctx-size = 32768" in text and "n-gpu-layers = 99" in text


def test_hybrid_split_temperature_and_embeddings_are_written(tmp_path):
    p = preset(tmp_path, "embed", gpu_layers=18, tensor_split=(0.7, 0.3), temperature=0.4, embedding=True)
    text = tunedmode.render([p])
    for line in ("n-gpu-layers = 18", "tensor-split = 0.7,0.3", "temp = 0.4", "embeddings = true"):
        assert line in text


def test_no_roles_is_an_error():
    with pytest.raises(ValueError, match="no GGUF role"):
        tunedmode.render([])


def test_the_generated_mode_renders_through_the_normal_mode_checks(tmp_path):
    p = preset(tmp_path, "chat")
    store = Store(tmp_path / "models.toml", tmp_path, tmp_path / "verified.json")
    add_local(store, p.model_path, name="chat")
    verifier = Verifier(load_manifest(store.manifest, tmp_path), tmp_path / "verified.json")
    tunedmode.write([p], tmp_path / "modes")
    rendered = modes.render_mode(
        "tuned",
        models_dir=tmp_path,
        state_dir=tmp_path / "state",
        verifier=verifier,
        user_dirs=[tmp_path / "modes"],
    )
    assert rendered.hal.main == "tuned-chat" and rendered.hal.context_window == 32768
    assert (tmp_path / "modes" / "tuned.ini").stat().st_mode & 0o777 == 0o600


def test_an_unpinned_weight_file_is_refused_by_the_mode_checks(tmp_path):
    p = preset(tmp_path, "chat")
    tunedmode.write([p], tmp_path / "modes")
    verifier = Verifier({}, tmp_path / "verified.json")
    with pytest.raises(modes.ModeError):
        modes.render_mode(
            "tuned",
            models_dir=tmp_path,
            state_dir=tmp_path / "state",
            verifier=verifier,
            user_dirs=[tmp_path / "modes"],
        )


def test_read_gives_back_every_preset_render_wrote(tmp_path):
    presets = [
        preset(tmp_path, "chat"),
        preset(
            tmp_path, "code-128k", context=131072, gpu_layers=30, tensor_split=(0.5, 0.5), temperature=0.2
        ),
        preset(tmp_path, "embed", embedding=True, mmproj=tmp_path / "mmproj.gguf"),
    ]
    path = tunedmode.write(presets, tmp_path)
    assert tunedmode.read(path) == presets
    assert tunedmode.read(tmp_path / "missing.ini") == []
    path.write_text("version = 1\n[tuned-chat]\nmodel = x\n")
    with pytest.raises(ValueError, match="not one hal tune writes"):
        tunedmode.read(path)


def test_experts_in_ram_are_written_and_read_back_and_fit_is_off(tmp_path):
    p = tunedmode.Preset("chat", tmp_path / "moe.gguf", 32768, "q8_0", ALL_LAYERS, cpu_moe=20)
    text = tunedmode.render([p])
    assert "n-cpu-moe = 20" in text and "n-gpu-layers = 99" in text
    assert "fit = off" in text  # llama.cpp must not re-fit numbers the user set past its margin
    target = tunedmode.write([p], tmp_path)
    assert tunedmode.read(target)[0].cpu_moe == 20


def test_a_draft_model_is_written_and_read_back(tmp_path):
    draft = write_gguf(tmp_path / "mtp.gguf", llama_like())
    p = preset(tmp_path, "writer", draft_path=draft, spec_type="draft-mtp", draft_max=5)
    text = tunedmode.render([p])
    wanted = (f"model-draft = {draft}", "spec-type = draft-mtp", "spec-draft-n-max = 5")
    for line in (*wanted, "n-gpu-layers-draft = 99", "cache-type-k-draft = q8_0"):
        assert line in text
    back = tunedmode.read(tunedmode.write([p], tmp_path))[0]
    assert (back.draft_path, back.spec_type, back.draft_max) == (draft, "draft-mtp", 5)


def test_parallel_slots_share_one_cache_sized_for_all_of_them(tmp_path):
    p = preset(tmp_path, "writer", parallel=4)
    text = tunedmode.render([p])
    assert "ctx-size = 131072" in text and "parallel = 4" in text and "kv-unified = true" in text
    back = tunedmode.read(tunedmode.write([p], tmp_path))[0]
    assert (back.context, back.parallel) == (32768, 4)  # the context is per request again


def test_a_model_drafting_with_its_own_mtp_layers_names_no_draft_file(tmp_path):
    p = preset(tmp_path, "code", spec_type="draft-mtp", draft_max=2)
    p = tunedmode.Preset(**{**p.__dict__, "draft_path": p.model_path})
    text = tunedmode.render([p])
    assert "spec-type = draft-mtp" in text and "spec-draft-n-max = 2" in text and "model-draft" not in text
    back = tunedmode.read(tunedmode.write([p], tmp_path))[0]
    assert back.draft_path == p.model_path and back.spec_type == "draft-mtp"

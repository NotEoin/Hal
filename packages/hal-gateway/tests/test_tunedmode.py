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

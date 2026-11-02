"""Tests for `halkit.discovery`, against a fake Ollama server on loopback and generated GGUF files."""

import pytest

from halkit import discovery
from halkit.modelconf import Source
from halkit.testing.gguf import llama_like, write_gguf
from halkit.testing.ollama import FakeOllama

GEMMA_SHOW = {
    "capabilities": ["completion", "vision", "tools"],
    "model_info": {
        "general.architecture": "gemma3",
        "gemma3.block_count": 48,
        "gemma3.attention.head_count": 16,
        "gemma3.attention.head_count_kv": 8,
        "gemma3.attention.key_length": 256,
        "gemma3.embedding_length": 3840,
        "gemma3.context_length": 131072,
    },
}
TAGS = [
    {
        "name": "gemma3:12b",
        "size": 8_149_190_253,
        "digest": "abc123",
        "details": {"family": "gemma3", "parameter_size": "12.2B", "quantization_level": "Q4_K_M"},
    },
    {"name": "qwen3:8b", "size": 5_200_000_000, "digest": "def456", "details": {"family": "qwen3"}},
    {"name": "broken:1b", "size": 1, "digest": "x", "details": {}},
]
QWEN_SHOW = {
    "capabilities": ["completion", "tools", "thinking"],
    "model_info": {"general.architecture": "qwen3"},
}


@pytest.fixture
def ollama():
    with FakeOllama(TAGS, {"gemma3:12b": GEMMA_SHOW, "qwen3:8b": QWEN_SHOW}) as server:
        yield server


def test_ollama_models_carry_size_architecture_and_capabilities(ollama):
    found = discovery.discover(Source("local", "ollama", ollama.url))
    gemma = next(m for m in found.models if m.name == "gemma3:12b")
    assert (gemma.size_bytes, gemma.params_b, gemma.quant) == (8_149_190_253, 12.2, "Q4_K_M")
    assert (gemma.layers, gemma.kv_heads, gemma.head_dim, gemma.context_native) == (48, 8, 256, 131072)
    assert gemma.capabilities == {"tools", "vision"} and gemma.digest == "abc123"
    assert gemma.has_architecture


def test_ollama_thinking_capability_is_reported(ollama):
    found = discovery.discover(Source("local", "ollama", ollama.url))
    assert "thinking" in next(m for m in found.models if m.name == "qwen3:8b").capabilities


def test_a_model_whose_show_fails_is_listed_with_a_note(ollama):
    found = discovery.discover(Source("local", "ollama", ollama.url))
    broken = next(m for m in found.models if m.name == "broken:1b")
    assert not broken.has_architecture and any("broken:1b" in n for n in found.notes)


def test_a_source_that_is_down_reports_an_error_not_an_exception():
    found = discovery.discover(Source("gone", "ollama", "http://127.0.0.1:9"))
    assert found.models == () and found.error


def test_openai_models_come_from_v1_models_with_optional_metadata():
    rows = [
        {
            "id": "qwen3-30b",
            "meta": {"n_ctx_train": 40960, "n_params": 30_000_000_000, "size": 18_000_000_000},
        },
        {"id": "plain-model"},
    ]
    with FakeOllama(openai_models=rows) as server:
        found = discovery.discover(Source("lms", "openai", server.url + "/v1"))
    qwen, plain = found.models
    assert (qwen.context_native, qwen.params_b, qwen.size_bytes) == (40960, 30.0, 18_000_000_000)
    assert plain.size_bytes is None and plain.layers is None and not plain.has_architecture


def test_openai_source_without_v1_in_its_url_gets_it_added():
    with FakeOllama(openai_models=[{"id": "m"}]) as server:
        found = discovery.discover(Source("lms", "openai", server.url))
        assert [m.name for m in found.models] == ["m"]
        assert server.requests[0][1] == "/v1/models"


def test_the_api_key_is_read_from_its_file_and_sent_as_a_bearer_token(tmp_path):
    key = tmp_path / "k"
    key.write_text("sk-test\n")
    key.chmod(0o600)
    with FakeOllama(openai_models=[{"id": "m"}]) as server:
        discovery.discover(Source("lms", "openai", server.url, api_key_file=str(key)))
        assert server.requests[0][2]["Authorization"] == "Bearer sk-test"


def test_a_key_file_readable_by_others_is_refused(tmp_path):
    key = tmp_path / "k"
    key.write_text("sk-test\n")
    key.chmod(0o644)
    found = discovery.discover(
        Source("c", "openai-cloud", api_key_file=str(key)), endpoints={"openai-cloud": "http://127.0.0.1:9"}
    )
    assert "chmod 600" in found.error


def test_anthropic_sends_its_own_headers(tmp_path):
    key = tmp_path / "k"
    key.write_text("ant-key")
    key.chmod(0o600)
    with FakeOllama(openai_models=[{"id": "claude-sonnet-4-5"}]) as server:
        found = discovery.discover(
            Source("claude", "anthropic", api_key_file=str(key)), endpoints={"anthropic": server.url}
        )
        headers = server.requests[0][2]
    assert headers["x-api-key"] == "ant-key" and headers["anthropic-version"]
    assert "thinking" in found.models[0].capabilities


def test_gguf_folder_is_read_from_headers_with_vision_from_mmproj(tmp_path):
    write_gguf(tmp_path / "llama-8b-Q4_K_M.gguf", llama_like(), pad_to=100_000)
    sub = tmp_path / "vl"
    sub.mkdir()
    write_gguf(sub / "vl-model.gguf", llama_like(layers=24), pad_to=100_000)
    write_gguf(sub / "mmproj-vl.gguf", {"general.architecture": "clip"})
    (tmp_path / "junk.gguf").write_bytes(b"not a gguf")
    found = discovery.discover(Source("weights", "gguf", path=str(tmp_path)))
    by_name = {m.name: m for m in found.models}
    assert set(by_name) == {"llama-8b-Q4_K_M", "vl-model"}
    llama = by_name["llama-8b-Q4_K_M"]
    assert (llama.layers, llama.kv_heads, llama.head_dim, llama.quant, llama.size_bytes) == (
        32,
        8,
        128,
        "Q4_K_M",
        100_000,
    )
    assert (llama.params_b, llama.context_native) == (8.0, 131072) and "tools" in llama.capabilities
    assert "vision" in by_name["vl-model"].capabilities and "vision" not in llama.capabilities
    assert any("junk.gguf" in n for n in found.notes)


def test_a_split_gguf_is_one_model_with_the_summed_size(tmp_path):
    for n in (1, 2, 3):
        write_gguf(tmp_path / f"big-0000{n}-of-00003.gguf", llama_like(), pad_to=100_000)
    found = discovery.discover(Source("w", "gguf", path=str(tmp_path)))
    assert [(m.name, m.size_bytes) for m in found.models] == [("big", 300_000)]


def test_a_missing_gguf_folder_is_an_error_string(tmp_path):
    assert "not a folder" in discovery.discover(Source("w", "gguf", path=str(tmp_path / "nope"))).error


def test_ollama_reads_sliding_window_and_hybrid_keys():
    # regression: the KV maths needs these to avoid costing every layer at full context
    gemma = {"model_info": {**GEMMA_SHOW["model_info"], "gemma3.attention.sliding_window": 1024}}
    qwen = {
        "model_info": {
            "general.architecture": "qwen35",
            "qwen35.block_count": 40,
            "qwen35.full_attention_interval": 4,
        }
    }
    tags = [{"name": "g:1", "size": 1}, {"name": "q:1", "size": 1}]
    with FakeOllama(tags, {"g:1": gemma, "q:1": qwen}) as server:
        found = {m.name: m for m in discovery.discover(Source("l", "ollama", server.url)).models}
    assert (found["g:1"].sliding_window, found["g:1"].attention_interval) == (1024, 6)
    assert (found["q:1"].sliding_window, found["q:1"].attention_interval) == (None, 4)


def test_gguf_reads_the_sliding_window_pattern(tmp_path):
    meta = {**llama_like(), "general.architecture": "gemma4"}
    meta = {k.replace("llama.", "gemma4."): v for k, v in meta.items()}
    meta |= {"gemma4.attention.sliding_window": 512, "gemma4.attention.sliding_window_pattern": 5}
    write_gguf(tmp_path / "g.gguf", meta, pad_to=100_000)
    model = discovery.discover(Source("w", "gguf", path=str(tmp_path))).models[0]
    assert (model.sliding_window, model.attention_interval) == (512, 5)


def test_a_model_listed_twice_is_one_entry_and_llamacpp_blobs_are_hidden():
    # regression: Ollama listed a model twice and `llamacpp:<sha>` blob ids, so pickers showed duplicates
    rows = [{"id": "gemma:12b"}, {"id": "llamacpp:" + "a" * 64}, {"id": "gemma:12b"}, {"id": "qwen:8b"}]
    with FakeOllama(openai_models=rows) as server:
        found = discovery.discover(Source("o", "openai", server.url))
    assert [m.name for m in found.models] == ["gemma:12b", "qwen:8b"]


def test_an_ollama_tag_list_with_a_repeated_name_gives_one_model():
    tags = [{"name": "qwen3:8b", "size": 1}, {"name": "qwen3:8b", "size": 1}]
    with FakeOllama(tags, {"qwen3:8b": QWEN_SHOW}) as server:
        found = discovery.discover(Source("l", "ollama", server.url))
    assert [m.name for m in found.models] == ["qwen3:8b"]


def test_gemma4_per_layer_kv_heads_and_window_pattern_are_summed_per_layer():
    # regression: Gemma 4 lists `head_count_kv` and `sliding_window_pattern` per layer, so the
    # single-value fields fell back to all query heads and a guessed pattern (4x to 8x too much KV)
    pattern = [True] * 5 + [False]
    info = {
        "general.architecture": "gemma4",
        "gemma4.block_count": 6,
        "gemma4.attention.head_count": 16,
        "gemma4.attention.head_count_kv": [8] * 5 + [2],
        "gemma4.attention.key_length": 512,
        "gemma4.attention.key_length_swa": 256,
        "gemma4.attention.sliding_window": 1024,
        "gemma4.attention.sliding_window_pattern": pattern,
    }
    with FakeOllama([{"name": "g4:1", "size": 1}], {"g4:1": {"model_info": info}}) as server:
        model = discovery.discover(Source("l", "ollama", server.url)).models[0]
    assert model.kv_full_width == 2 * (512 + 512)
    assert model.kv_window_width == 5 * 8 * (256 + 256)


def test_an_ollama_tag_records_the_model_it_was_built_from():
    tags = [{"name": "hal-fast:latest", "size": 1, "details": {"parent_model": "qwen3.5:4b"}}]
    with FakeOllama(tags, {}) as server:
        model = discovery.discover(Source("l", "ollama", server.url)).models[0]
    assert model.base == "qwen3.5:4b"

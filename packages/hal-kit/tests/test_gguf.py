"""Tests for `halkit.gguf`, on generated header-only files."""

import struct

import pytest

from halkit import gguf
from halkit.testing.gguf import llama_like, write_gguf


def test_header_scalars_and_strings_are_read(tmp_path):
    header = gguf.read_header(write_gguf(tmp_path / "m.gguf", llama_like()))
    assert (header.version, header.architecture, header.quantisation) == (3, "llama", "Q4_K_M")
    assert header.arch_int("block_count") == 32
    assert header.metadata["general.size_label"] == "8B"


def test_a_large_vocabulary_array_is_skipped_not_stored(tmp_path):
    header = gguf.read_header(write_gguf(tmp_path / "m.gguf", llama_like()))
    assert "tokenizer.ggml.tokens" not in header.metadata
    assert header.metadata["tokenizer.chat_template"].startswith("{%")


def test_a_per_layer_array_gives_its_largest_entry(tmp_path):
    meta = {"general.architecture": "x", "x.attention.head_count_kv": [2, 8, 4]}
    assert gguf.read_header(write_gguf(tmp_path / "m.gguf", meta)).arch_int("attention.head_count_kv") == 8


def test_only_the_header_is_read_from_a_large_file(tmp_path):
    path = write_gguf(tmp_path / "m.gguf", llama_like(), pad_to=50 * 1024 * 1024)
    reads = []
    real_open = type(path).open

    def spy(self, *a, **k):
        fh = real_open(self, *a, **k)
        real_read = fh.read
        fh.read = lambda n=-1: reads.append(n) or real_read(n)
        return fh

    type(path).open = spy
    try:
        gguf.read_header(path)
    finally:
        type(path).open = real_open
    assert -1 not in reads and sum(reads) < 1024 * 1024


def test_bad_magic_is_refused(tmp_path):
    (tmp_path / "x.gguf").write_bytes(b"NOPE" + b"\0" * 32)
    with pytest.raises(gguf.GgufError, match="bad magic"):
        gguf.read_header(tmp_path / "x.gguf")


def test_a_truncated_header_is_refused(tmp_path):
    path = write_gguf(tmp_path / "x.gguf", llama_like())
    path.write_bytes(path.read_bytes()[:60])
    with pytest.raises(gguf.GgufError):
        gguf.read_header(path)


def test_an_unsupported_version_is_refused(tmp_path):
    path = tmp_path / "x.gguf"
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 1, 0, 0))
    with pytest.raises(gguf.GgufError, match="version"):
        gguf.read_header(path)


def test_a_huge_key_count_is_refused_without_looping(tmp_path):
    path = tmp_path / "x.gguf"
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 10**12))
    with pytest.raises(gguf.GgufError, match="metadata keys"):
        gguf.read_header(path)


def test_a_skipped_array_keeps_its_length(tmp_path):
    header = gguf.read_header(write_gguf(tmp_path / "m.gguf", llama_like()))
    assert header.array_lengths["tokenizer.ggml.tokens"] == 2000


def test_tensor_sizes_come_from_the_offsets(tmp_path):
    tensors = {"token_embd.weight": 1000, "blk.0.ffn_up_exps.weight": 64, "output.weight": 40}
    header = gguf.read_header(write_gguf(tmp_path / "m.gguf", llama_like(), tensors=tensors))
    # each size is rounded up to the 32-byte alignment except the last, which runs to the end
    assert header.tensor_bytes == {
        "token_embd.weight": 1024,
        "blk.0.ffn_up_exps.weight": 64,
        "output.weight": 40,
    }


def test_a_tensor_offset_past_the_end_is_refused(tmp_path):
    path = write_gguf(tmp_path / "m.gguf", llama_like(), tensors={"a": 64, "b": 64})
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(gguf.GgufError, match="past the end"):
        gguf.read_header(path)

"""Writes a tiny GGUF file (header and tensor table, zero-filled data) for tests of `halkit.gguf`."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from pathlib import Path

_INT = 4  # uint32
_FLOAT = 6
_BOOL = 7
_STRING = 8
_ARRAY = 9
_UINT64 = 10


def _s(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _value(value: object) -> tuple[int, bytes]:
    if isinstance(value, bool):
        return _BOOL, struct.pack("<?", value)
    if isinstance(value, int):
        return (_INT, struct.pack("<I", value)) if value < 2**32 else (_UINT64, struct.pack("<Q", value))
    if isinstance(value, float):
        return _FLOAT, struct.pack("<f", value)
    if isinstance(value, str):
        return _STRING, _s(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return _ARRAY, struct.pack("<IQ", _STRING, len(value)) + b"".join(_s(v) for v in value)
    if isinstance(value, list):
        return _ARRAY, struct.pack("<IQ", _INT, len(value)) + b"".join(struct.pack("<I", v) for v in value)
    raise TypeError(f"cannot write {value!r}")


ALIGNMENT = 32  # the GGUF default `general.alignment`
F32 = 0  # ggml type id; the reader takes sizes from offsets, so the type is only a placeholder


def write_gguf(
    path: Path,
    metadata: Mapping[str, object],
    *,
    version: int = 3,
    pad_to: int = 0,
    tensors: Mapping[str, int] | None = None,
) -> Path:
    """Writes `metadata` as a GGUF v`version` header, then `tensors` (name -> bytes) as zero-filled data.

    `pad_to` appends zero bytes to reach a size; with no tensors that padding belongs to no tensor.
    """
    body = b""
    for key, value in metadata.items():
        kind, raw = _value(value)
        body += _s(key) + struct.pack("<I", kind) + raw
    table, offset = b"", 0
    for name, size in (tensors or {}).items():
        table += _s(name) + struct.pack("<IQIQ", 1, size, F32, offset)
        offset += -(-size // ALIGNMENT) * ALIGNMENT
    head = b"GGUF" + struct.pack("<IQQ", version, len(tensors or {}), len(metadata)) + body + table
    head += b"\0" * (-len(head) % ALIGNMENT if tensors else 0)
    if tensors:
        last = list(tensors.values())[-1]
        head += b"\0" * (offset - (-(-last // ALIGNMENT) * ALIGNMENT) + last)
    Path(path).write_bytes(head + b"\0" * max(pad_to - len(head), 0))
    return Path(path)


def llama_like(
    layers: int = 32, heads: int = 32, kv_heads: int = 8, emb: int = 4096, ctx: int = 131072
) -> dict[str, object]:
    """Metadata for a small llama-architecture model, with a vocabulary array to be skipped."""
    return {
        "general.architecture": "llama",
        "general.name": "Tiny Test",
        "general.file_type": 15,
        "general.size_label": "8B",
        "llama.block_count": layers,
        "llama.attention.head_count": heads,
        "llama.attention.head_count_kv": kv_heads,
        "llama.embedding_length": emb,
        "llama.context_length": ctx,
        "tokenizer.ggml.tokens": [f"tok{i}" for i in range(2000)],
        "tokenizer.chat_template": "{% if tools %}tools{% endif %}",
    }

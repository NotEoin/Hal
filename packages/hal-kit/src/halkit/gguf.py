"""A small reader for the key-value section and tensor table of a GGUF header.

`read_header` opens a `.gguf` file and reads only its metadata and tensor table, never the
tensors, so a multi-gigabyte model costs a few kilobytes of I/O (a few megabytes with a large
tokenizer). Design:

- Layout (llama.cpp `gguf` spec, versions 2 and 3): magic `GGUF`, `uint32` version, `uint64`
  tensor count, `uint64` key-value count, then each key (length-prefixed string), a `uint32`
  type and the value. All integers are little-endian.
- Strings over `MAX_STRING_BYTES` and arrays over `MAX_ARRAY_ITEMS` are skipped, not stored:
  the tokenizer vocabulary is hundreds of thousands of entries and nothing here needs it.
  A skipped value is absent from `Header.metadata`; the length of a skipped array is kept in
  `Header.array_lengths`, because the vocabulary size matters to the memory estimate.
- The tensor table follows the metadata: per tensor a name, `uint32` dimension count, `uint64`
  dimensions, `uint32` type and `uint64` offset into the data section. A tensor's size is the
  distance to the next offset (the last one runs to the end of the file), so no table of ggml
  block sizes is needed and a new quantisation type costs nothing here.
- Malformed input raises `GgufError`; a hostile file cannot make the reader allocate more than
  `MAX_STRING_BYTES` or loop more than `MAX_KEYS` times.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

MAGIC = b"GGUF"
SUPPORTED_VERSIONS = (2, 3)
MAX_KEYS = 100_000  # real files carry under 1000; the cap stops a corrupt count
MAX_STRING_BYTES = 4 * 1024 * 1024  # a chat template is some kilobytes
MAX_ARRAY_ITEMS = 1024  # per-layer arrays are shorter than this; vocabularies are not
MAX_SKIPPED_STRINGS = 2_000_000  # a vocabulary walk needs one read per entry; stop a runaway
MAX_TENSORS = 200_000  # a 1T-parameter MoE has some tens of thousands; the cap stops a corrupt count
MAX_DIMS = 8  # ggml tensors have at most 4 dimensions
DEFAULT_ALIGNMENT = 32  # `general.alignment` when the file does not set it (GGUF spec)
# value type id -> (struct format, size); strings (8) and arrays (9) are handled apart
_SCALARS = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
    6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}  # fmt: skip
T_STRING, T_ARRAY = 8, 9
# `general.file_type` ids from llama.cpp `llama_ftype`, as the usual quantisation names
FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K",
    11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S",
    17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS",
    23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
    29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
}  # fmt: skip


class GgufError(ValueError):
    """The file is not a readable GGUF header."""


@dataclass(frozen=True)
class Header:
    """The metadata of a GGUF file."""

    version: int
    tensor_count: int
    metadata: dict[str, object] = field(default_factory=dict)
    array_lengths: dict[str, int] = field(default_factory=dict)  # skipped arrays: key -> item count
    tensor_bytes: dict[str, int] = field(default_factory=dict)  # tensor name -> bytes in this file

    @property
    def architecture(self) -> str:
        """`general.architecture`, or an empty string."""
        return str(self.metadata.get("general.architecture", ""))

    def arch_int(self, suffix: str) -> int | None:
        """`<architecture>.<suffix>` as an int; a per-layer list gives its largest entry."""
        value = self.metadata.get(f"{self.architecture}.{suffix}")
        if isinstance(value, list):
            numbers = [v for v in value if isinstance(v, int) and not isinstance(v, bool)]
            return max(numbers) if numbers else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def quantisation(self) -> str:
        """The quantisation name from `general.file_type`, or an empty string."""
        ftype = self.metadata.get("general.file_type")
        return FILE_TYPES.get(ftype, "") if isinstance(ftype, int) else ""


def _read(fh: BinaryIO, count: int) -> bytes:
    data = fh.read(count)
    if len(data) != count:
        raise GgufError("file ends inside the header")
    return data


def _unpack(fh: BinaryIO, fmt: str, size: int) -> object:
    return struct.unpack(fmt, _read(fh, size))[0]


def _string(fh: BinaryIO, *, keep: bool = True) -> str | None:
    length = int(_unpack(fh, "<Q", 8))  # type: ignore[call-overload]  # struct returns int here
    if length > MAX_STRING_BYTES or not keep:
        fh.seek(length, 1)
        return None
    return _read(fh, length).decode("utf-8", "replace")


def _value(fh: BinaryIO, kind: int, budget: list[int]) -> tuple[object, int]:
    """Reads one value; returns `(value, 0)`, or `(None, item count)` for a skipped string or array."""
    if kind in _SCALARS:
        return _unpack(fh, *_SCALARS[kind]), 0
    if kind == T_STRING:
        return _string(fh), 0
    if kind != T_ARRAY:
        raise GgufError(f"unknown value type {kind}")
    item_kind = int(_unpack(fh, "<I", 4))  # type: ignore[call-overload]
    count = int(_unpack(fh, "<Q", 8))  # type: ignore[call-overload]
    if item_kind in _SCALARS:
        fmt, size = _SCALARS[item_kind]
        if count > MAX_ARRAY_ITEMS:
            fh.seek(size * count, 1)
            return None, count
        return [struct.unpack(fmt, _read(fh, size))[0] for _ in range(count)], 0
    if item_kind != T_STRING:
        raise GgufError(f"unsupported array of type {item_kind}")
    budget[0] -= count
    if budget[0] < 0:
        raise GgufError("too many strings in arrays")
    for _ in range(count):
        _string(fh, keep=False)
    return None, count


def _tensor_table(fh: BinaryIO, count: int, alignment: int, file_size: int) -> dict[str, int]:
    """Name -> bytes of every tensor, from the table that follows the metadata."""
    if count > MAX_TENSORS:
        raise GgufError(f"the file claims {count} tensors; limit {MAX_TENSORS}")
    offsets: list[tuple[int, str]] = []
    for _ in range(count):
        name = _string(fh)
        dims = int(_unpack(fh, "<I", 4))  # type: ignore[call-overload]
        if name is None or dims > MAX_DIMS:
            raise GgufError("a tensor entry is malformed")
        fh.seek(8 * dims + 4, 1)  # the dimensions and the type; the size comes from the offsets
        offsets.append((int(_unpack(fh, "<Q", 8)), name))  # type: ignore[call-overload]
    if not offsets:
        return {}
    if alignment < 1:
        raise GgufError(f"general.alignment {alignment} is not positive")
    start = -(-fh.tell() // alignment) * alignment
    offsets.sort()
    ends = [off for off, _ in offsets[1:]] + [file_size - start]
    sizes = {name: end - off for (off, name), end in zip(offsets, ends, strict=True)}
    if any(size < 0 for size in sizes.values()):
        raise GgufError("tensor offsets run past the end of the file")
    return sizes


def read_header(path: Path) -> Header:
    """Reads the metadata and tensor table of the GGUF file at `path`; raises `GgufError` or `OSError`."""
    file_size = Path(path).stat().st_size
    with Path(path).open("rb") as fh:
        if _read(fh, 4) != MAGIC:
            raise GgufError(f"{path} is not a GGUF file (bad magic)")
        version = int(_unpack(fh, "<I", 4))  # type: ignore[call-overload]
        if version not in SUPPORTED_VERSIONS:
            raise GgufError(f"{path} has GGUF version {version}; supported: {SUPPORTED_VERSIONS}")
        tensors = int(_unpack(fh, "<Q", 8))  # type: ignore[call-overload]
        keys = int(_unpack(fh, "<Q", 8))  # type: ignore[call-overload]
        if keys > MAX_KEYS:
            raise GgufError(f"{path} claims {keys} metadata keys; limit {MAX_KEYS}")
        budget = [MAX_SKIPPED_STRINGS]
        meta: dict[str, object] = {}
        lengths: dict[str, int] = {}
        for _ in range(keys):
            key = _string(fh)
            kind = int(_unpack(fh, "<I", 4))  # type: ignore[call-overload]
            if key is None:
                raise GgufError("a metadata key is too long")
            value, skipped = _value(fh, kind, budget)
            if value is not None:
                meta[key] = value
            elif skipped:
                lengths[key] = skipped
        alignment = meta.get("general.alignment", DEFAULT_ALIGNMENT)
        sizes = _tensor_table(
            fh, tensors, alignment if isinstance(alignment, int) else DEFAULT_ALIGNMENT, file_size
        )
    return Header(version, tensors, meta, lengths, sizes)

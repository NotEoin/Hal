"""`ModelInfo`: what Hal can learn about one model, whatever source it came from.

The tuning maths (`halkit.tuning`) and the effort table (`halkit.effort`) take this record, so
they do not care whether the facts came from a GGUF header, Ollama or an OpenAI-compatible
server. A field is `None` (or empty) when the source does not say.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CAPABILITIES = ("tools", "vision", "thinking", "embedding")


@dataclass(frozen=True)
class ModelInfo:
    """One model of one source."""

    source: str  # name of the source in `[models.sources]`
    name: str  # the name the source uses: an Ollama tag, a GGUF file name, an API model id
    kind: str  # the source kind: ollama, openai, gguf, anthropic or openai-cloud
    size_bytes: int | None = None  # weights on disk
    params_b: float | None = None  # parameters in billions
    quant: str = ""
    family: str = ""  # architecture or model family, lowercase: "qwen3", "gemma3", "llama"
    layers: int | None = None
    heads: int | None = None
    kv_heads: int | None = None
    head_dim: int | None = None  # size of one key head; the value head is assumed the same
    embedding: int | None = None
    context_native: int | None = None
    sliding_window: int | None = None  # tokens a sliding-window layer attends to (Gemma 3 and 4)
    # every Nth layer is full (or global) attention: Gemma's pattern, Qwen3.5's `full_attention_interval`
    attention_interval: int | None = None
    # K+V elements per token summed over full and over sliding-window layers, when the metadata is
    # per-layer (Gemma 4 lists KV heads and the window pattern per layer, with smaller window heads)
    kv_full_width: int | None = None
    kv_window_width: int | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    path: str = ""  # GGUF file path; empty for other kinds
    digest: str = ""  # Ollama digest, when known

    @property
    def has_architecture(self) -> bool:
        """True when layers, KV heads and head size are all known, so KV memory can be computed."""
        return bool(self.layers and self.kv_heads and self.head_dim)

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready view."""
        data = dict(self.__dict__)
        data["capabilities"] = sorted(self.capabilities)
        return data

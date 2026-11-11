"""Model discovery: list the models of a source and what can be learnt about each.

`discover(source)` returns the source's `ModelInfo` records. It never raises: a source that is
down, a bad key file or an unreadable GGUF becomes an `error` string on the result, so
`hal models` can show the other sources. Design decisions:

- Ollama: `GET /api/tags` for names, sizes and digests, then `POST /api/show` per model for
  `model_info` (layers, heads, KV heads, key length, context), `capabilities` and the
  `num_ctx` parameter, which is the context Ollama serves the model with.
- GGUF: the header and tensor table of each file are read with `halkit.gguf` (no weights are
  read). A draft model for speculative decoding (a file or folder named for MTP, an assistant,
  DFlash, DSpark, EAGLE or a draft) is not listed as a model either: it is attached to the models
  of its folder or the folder above (`MTP/` beside the weights), and when both name a size such
  as `31b`, only to the model of that size.
  A `mmproj` file beside a model marks it as vision-capable and is not listed as a model
  itself. A split model (`-00001-of-00003.gguf`) is one entry whose size is the sum of its
  shards; the tensor sizes of every shard are read, so expert and embedding bytes are exact.
- OpenAI-compatible: `GET {url}/models`. Metadata is unknown unless the server adds it: llama.cpp
  server reports `meta.n_ctx_train`, `meta.n_params` and `meta.size`, vLLM reports `max_model_len`.
- Anthropic and OpenAI cloud: model ids only; the cloud hosts are fixed constants.
- A list is cleaned once, in `discover`: a name a source repeats is one entry (Ollama's
  `/api/tags` and `/v1/models` can list a model twice), and `llamacpp:<sha>` ids are dropped (Ollama
  lists the blob of a model it runs through llama.cpp under a hash, with no name to show).
- Every HTTP call has a timeout and a response size cap.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import effort, gguf, secrets
from .modelconf import Source
from .modelinfo import ModelInfo

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 10.0
SHOW_TIMEOUT_S = 15.0  # `/api/show` reads the model's metadata from disk
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_GGUF_FILES = 2000  # a models folder with more is a mistake in `path`
CLOUD_URLS = {"anthropic": "https://api.anthropic.com/v1", "openai-cloud": "https://api.openai.com/v1"}
BLOB_PREFIX = "llamacpp:"  # Ollama's `/v1/models` ids for blobs run by its llama.cpp runner
ANTHROPIC_VERSION = "2023-06-01"
SHARD = re.compile(r"-(\d{5})-of-(\d{5})$")
SIZE_LABEL = re.compile(r"(\d+(?:\.\d+)?)\s*([MBT])", re.IGNORECASE)
PARAM_UNITS = {"M": 1e-3, "B": 1.0, "T": 1000.0}
# Gemma 3 mixes 5 sliding-window layers with 1 global layer (Gemma 3 technical report, 2025); used
# when a Gemma GGUF has a window but no `sliding_window_pattern` key
GEMMA_DEFAULT_PATTERN = 6
EMBEDDING_ARCHS = ("bert", "nomic-bert", "jina-bert", "modern-bert", "t5encoder")


class DiscoveryError(OSError):
    """A source did not answer or answered something unreadable."""


@dataclass(frozen=True)
class Discovery:
    """The models found in one source; `error` says why the list may be incomplete."""

    source: str
    models: tuple[ModelInfo, ...] = ()
    error: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


# Fetches `(method, url, headers, body)` and returns the parsed JSON; swapped in by tests that
# do not want a socket at all.
Fetch = Callable[[str, str, Mapping[str, str], object, float], object]


def http_json(method: str, url: str, headers: Mapping[str, str], body: object, timeout: float) -> object:
    """One JSON request with a timeout; raises `DiscoveryError`."""
    parts = urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    payload = None if body is None else json.dumps(body).encode()
    heads = {"Accept": "application/json", **headers}
    if payload is not None:
        heads["Content-Type"] = "application/json"
    conn = cls(parts.hostname or "", parts.port, timeout=timeout)
    try:
        conn.request(method, target, body=payload, headers=heads)
        resp = conn.getresponse()
        raw = resp.read(MAX_BODY_BYTES + 1)
        if resp.status >= 400:
            raise DiscoveryError(f"{url}: HTTP {resp.status}")
        if len(raw) > MAX_BODY_BYTES:
            raise DiscoveryError(f"{url}: reply larger than {MAX_BODY_BYTES} bytes")
        return json.loads(raw)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise DiscoveryError(f"{url}: {exc}") from exc
    finally:
        conn.close()


def _params_b(label: object) -> float | None:
    """Billions of parameters from `8B`, `30B-A3B` (the first number), `600M`, or `12.2B`."""
    found = SIZE_LABEL.search(str(label or ""))
    return float(found.group(1)) * PARAM_UNITS[found.group(2).upper()] if found else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _attention_shape(arch: str, get: Callable[[str], int | None]) -> tuple[int | None, int | None]:
    """`(sliding_window, attention_interval)` from the `<arch>.` metadata keys that `get` reads.

    Keys follow llama.cpp: `attention.sliding_window` with `attention.sliding_window_pattern`
    (Gemma) and `full_attention_interval` (Qwen3.5 hybrids). Both are None for plain attention.
    """
    window = get("attention.sliding_window")
    interval = get("attention.sliding_window_pattern") or get("full_attention_interval")
    if window and not interval and arch.startswith("gemma"):
        interval = GEMMA_DEFAULT_PATTERN
    return window, interval


def _layer_widths(layers: int | None, get: Callable[[str], object]) -> tuple[int | None, int | None]:
    """`(full, window)`: K+V elements per token over full-attention and sliding-window layers.

    Only for per-layer metadata (a list `attention.sliding_window_pattern` or
    `attention.head_count_kv`, as Gemma 4 has), where one head count and head size would be wrong;
    window layers use `key_length_swa` and `value_length_swa` when present. `(None, None)` otherwise.
    """
    pattern, kv = get("attention.sliding_window_pattern"), get("attention.head_count_kv")
    if not layers or not isinstance(pattern, list) or len(pattern) != layers:
        return None, None
    if isinstance(kv, list) and len(kv) != layers:
        return None, None
    key = _int(get("attention.key_length"))
    if not key:
        return None, None
    value = _int(get("attention.value_length")) or key
    key_swa = _int(get("attention.key_length_swa")) or key
    value_swa = _int(get("attention.value_length_swa")) or key_swa
    full = window = 0
    for i, sliding in enumerate(pattern):
        heads = _int(kv[i]) if isinstance(kv, list) else _int(kv)
        if not heads:
            return None, None
        if sliding:
            window += heads * (key_swa + value_swa)
        else:
            full += heads * (key + value)
    return full, window


def _ollama(source: Source, fetch: Fetch) -> Discovery:
    base = source.url.rstrip("/")
    tags = fetch("GET", f"{base}/api/tags", {}, None, HTTP_TIMEOUT_S)
    models, notes = [], []
    for entry in (tags.get("models") or []) if isinstance(tags, dict) else []:
        name = str(entry.get("name") or entry.get("model") or "")
        if not name:
            continue
        details = entry.get("details") or {}
        info: dict = {}  # type: ignore[type-arg]
        served = None
        try:
            show = fetch("POST", f"{base}/api/show", {}, {"model": name}, SHOW_TIMEOUT_S)
            info = show.get("model_info") or {} if isinstance(show, dict) else {}
            caps = {str(c) for c in (show.get("capabilities") or [])} if isinstance(show, dict) else set()
            served = _num_ctx(show.get("parameters") if isinstance(show, dict) else None)
        except DiscoveryError as exc:
            notes.append(f"{name}: no metadata ({exc})")
            caps = set()
        arch = str(info.get("general.architecture") or details.get("family") or "").lower()
        heads = _int(info.get(f"{arch}.attention.head_count"))
        emb = _int(info.get(f"{arch}.embedding_length"))
        kv_heads = _int(info.get(f"{arch}.attention.head_count_kv")) or heads
        head_dim = _int(info.get(f"{arch}.attention.key_length")) or (emb // heads if emb and heads else None)
        window, interval = _attention_shape(arch, lambda suffix, i=info, a=arch: _int(i.get(f"{a}.{suffix}")))
        widths = _layer_widths(
            _int(info.get(f"{arch}.block_count")), lambda suffix, i=info, a=arch: i.get(f"{a}.{suffix}")
        )
        wanted = {"tools", "vision", "thinking", "embedding"}
        found = caps & wanted
        if not caps and effort.capability(arch, name).kind != "none":
            found.add("thinking")  # old Ollama reports no capabilities; fall back to the family table
        models.append(
            ModelInfo(
                source=source.name,
                name=name,
                kind="ollama",
                size_bytes=_int(entry.get("size")),
                params_b=_params_b(details.get("parameter_size") or info.get("general.size_label")),
                quant=str(details.get("quantization_level") or ""),
                base=str(details.get("parent_model") or ""),
                family=arch,
                layers=_int(info.get(f"{arch}.block_count")),
                heads=heads,
                kv_heads=kv_heads,
                head_dim=head_dim,
                embedding=emb,
                context_native=_int(info.get(f"{arch}.context_length")),
                sliding_window=window,
                attention_interval=interval,
                kv_full_width=widths[0],
                kv_window_width=widths[1],
                vocab=_int(info.get(f"{arch}.vocab_size")),
                experts=_int(info.get(f"{arch}.expert_count")),
                context_served=served,
                capabilities=frozenset(found),
                digest=str(entry.get("digest") or ""),
            )
        )
    return Discovery(source.name, tuple(models), notes=tuple(notes))


def _key_headers(source: Source) -> dict[str, str]:
    if not source.api_key_file:
        return {}
    key = secrets.read_secret(Path(source.api_key_file).expanduser())
    if source.kind == "anthropic":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {key}"}


def _openai(source: Source, fetch: Fetch, endpoints: Mapping[str, str]) -> Discovery:
    if source.kind in CLOUD_URLS:
        base = endpoints.get(source.kind, CLOUD_URLS[source.kind])
    else:
        base = source.url.rstrip("/")
        base = base if base.endswith("/v1") else f"{base}/v1"
    query = "?limit=1000" if source.kind == "anthropic" else ""  # the API pages at 20 by default
    doc = fetch("GET", f"{base}/models{query}", _key_headers(source), None, HTTP_TIMEOUT_S)
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise DiscoveryError(f"{base}/models: no 'data' list in the reply")
    models = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        name = str(row["id"])
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        cap = effort.capability("", name)
        params = meta.get("n_params")
        models.append(
            ModelInfo(
                source=source.name,
                name=name,
                kind=source.kind,
                size_bytes=_int(meta.get("size")),
                params_b=params / 1e9 if isinstance(params, int | float) and params > 0 else None,
                embedding=_int(meta.get("n_embd")),
                context_native=_int(meta.get("n_ctx_train"))
                or _int(row.get("max_model_len"))
                or _int(row.get("context_length")),
                capabilities=frozenset({"thinking"} if cap.kind != "none" else ()),
            )
        )
    return Discovery(source.name, tuple(models))


def _num_ctx(parameters: object) -> int | None:
    """The `num_ctx` line of Ollama's `parameters` text (`num_ctx    65536`), else None."""
    for line in str(parameters or "").splitlines():
        key, _, value = line.strip().partition(" ")
        if key == "num_ctx":
            return int(value.strip()) if value.strip().isdigit() else None
    return None


def _tensor_facts(
    tensors: Mapping[str, int], blocks: int | None, nextn: int | None
) -> tuple[int | None, ...]:
    """`(expert bytes, host bytes, idle bytes)` from tensor sizes; None where the table says nothing.

    Routed experts are the `*_exps` tensors (shared experts, `*_shexp`, are not routed). The input
    embedding stays in RAM only when a separate `output.weight` exists (llama.cpp copies a tied
    embedding to the GPU for the output layer). The last `nextn` blocks are multi-token-prediction
    layers, which llama.cpp loads only for a draft.
    """
    if not tensors:
        return None, None, None
    experts = sum(size for name, size in tensors.items() if "_exps." in name)
    host = tensors.get("token_embd.weight", 0) if "output.weight" in tensors else 0
    idle = 0
    if blocks and nextn and 0 < nextn < blocks:
        prefixes = tuple(f"blk.{i}." for i in range(blocks - nextn, blocks))
        idle = sum(size for name, size in tensors.items() if name.startswith(prefixes))
    return experts or None, host, idle


def _gguf_info(
    source: Source, path: Path, size: int, vision: bool, shards: tuple[Path, ...] = ()
) -> ModelInfo:
    head = gguf.read_header(path)
    tensors = dict(head.tensor_bytes)
    for shard in shards:
        if shard != path:
            tensors.update(gguf.read_header(shard).tensor_bytes)
    expert_bytes, host_bytes, idle_bytes = _tensor_facts(
        tensors, head.arch_int("block_count"), head.arch_int("nextn_predict_layers")
    )
    arch = head.architecture
    heads = head.arch_int("attention.head_count")
    emb = head.arch_int("embedding_length")
    head_dim = head.arch_int("attention.key_length") or (emb // heads if emb and heads else None)
    window, interval = _attention_shape(arch, head.arch_int)
    widths = _layer_widths(head.arch_int("block_count"), lambda suffix: head.metadata.get(f"{arch}.{suffix}"))
    template = str(head.metadata.get("tokenizer.chat_template", ""))
    caps = set()
    if "tools" in template:
        caps.add("tools")
    if effort.capability(arch, path.name).kind != "none" or "enable_thinking" in template:
        caps.add("thinking")
    if vision:
        caps.add("vision")
    if arch in EMBEDDING_ARCHS or head.arch_int("pooling_type") is not None:
        caps.add("embedding")
    return ModelInfo(
        source=source.name,
        name=SHARD.sub("", path.stem),
        kind="gguf",
        size_bytes=size,
        params_b=_params_b(head.metadata.get("general.size_label")),
        quant=head.quantisation,
        family=arch,
        layers=head.arch_int("block_count"),
        heads=heads,
        kv_heads=head.arch_int("attention.head_count_kv") or heads,
        head_dim=head_dim,
        embedding=emb,
        context_native=head.arch_int("context_length"),
        sliding_window=window,
        attention_interval=interval,
        kv_full_width=widths[0],
        kv_window_width=widths[1],
        vocab=head.array_lengths.get("tokenizer.ggml.tokens") or head.arch_int("vocab_size"),
        experts=head.arch_int("expert_count"),
        expert_bytes=expert_bytes,
        host_bytes=host_bytes,
        idle_bytes=idle_bytes,
        capabilities=frozenset(caps),
        path=str(path),
    )


DRAFT_NAME = re.compile(r"mtp|assistant|dflash|dspark|eagle|draft", re.IGNORECASE)
DRAFT_ORDER = ("mtp", "assistant", "dflash", "dspark", "eagle", "draft")  # best first, from measured speed
# a size label in a file name: `31b`, `27B`, `e2b` (Gemma's effective sizes), `0.8b`
SIZE_TOKEN = re.compile(r"(?<![a-z0-9])([a-z]?\d+(?:\.\d+)?b)(?![a-z0-9])", re.IGNORECASE)
NOT_A_MODEL = re.compile(r"^(mmproj|imatrix)", re.IGNORECASE)  # projectors and quantisation calibration data


DRAFT_FOLDER = re.compile(r"(mtp|assistant|dflash\d*|dspark|eagle\d*|drafts?)", re.IGNORECASE)


def is_draft(path: Path) -> bool:
    """True for a draft model file: its name names a draft kind, or its folder is named for one (`MTP/`).

    A whole model with its own multi-token-prediction layers is often named `...-MTP-Q4_K_M`,
    so a file whose blocks go past its `nextn` layers is a model, not a draft.
    """
    if not (DRAFT_NAME.search(path.name) or DRAFT_FOLDER.fullmatch(path.parent.name)):
        return False
    try:
        head = gguf.read_header(path)
    except (gguf.GgufError, OSError):
        return True  # unreadable: the name decides, and the model list reports nothing for it
    nextn = head.arch_int("nextn_predict_layers") or 0
    blocks = {name.split(".")[1] for name in head.tensor_bytes if name.startswith("blk.")}
    return not (nextn and len(blocks) > nextn)


def _draft_rank(path: Path) -> int:
    text = f"{path.parent.name} {path.name}".lower()
    return next((i for i, kind in enumerate(DRAFT_ORDER) if kind in text), len(DRAFT_ORDER))


def _drafts_for(model: Path, drafts: list[Path]) -> tuple[tuple[str, int], ...]:
    """The drafts of `model`: in its folder or one below, of the same size when both name one."""
    size = SIZE_TOKEN.search(model.name)
    mine = []
    for draft in drafts:
        if draft.parent != model.parent and draft.parent.parent != model.parent:
            continue
        other = SIZE_TOKEN.search(draft.name)
        if size and other and size.group(1).lower() != other.group(1).lower():
            continue
        mine.append(draft)
    mine.sort(key=lambda d: (_draft_rank(d), d.name))
    return tuple((str(d), d.stat().st_size) for d in mine)


def _gguf(source: Source) -> Discovery:
    root = Path(source.path).expanduser()
    if not root.is_dir():
        raise DiscoveryError(f"{root} is not a folder")
    files = sorted(root.rglob("*.gguf"))[:MAX_GGUF_FILES]
    projectors = {f.parent for f in files if f.name.lower().startswith("mmproj")}
    drafts = [f for f in files if is_draft(f) and not NOT_A_MODEL.match(f.name)]
    models, notes = [], []
    for path in files:
        if NOT_A_MODEL.match(path.name) or is_draft(path):
            continue
        shard = SHARD.search(path.stem)
        if shard and shard.group(1) != "00001":
            continue
        if shard:
            pattern = SHARD.sub("", path.stem) + "-*-of-" + shard.group(2) + ".gguf"
            parts = tuple(sorted(path.parent.glob(pattern)))
            size = sum(p.stat().st_size for p in parts)
        else:
            parts, size = (), path.stat().st_size
        try:
            info = _gguf_info(source, path, size, path.parent in projectors, parts)
            own = ((str(path), info.idle_bytes),) if info.idle_bytes else ()  # its built-in MTP layers
            models.append(replace(info, drafts=own + _drafts_for(path, drafts)))
        except (gguf.GgufError, OSError) as exc:
            notes.append(f"{path.name}: {exc}")
    return Discovery(source.name, tuple(models), notes=tuple(notes))


def _tidy(found: Discovery) -> Discovery:
    """`found` without repeated names and without `llamacpp:<sha>` blob ids, in source order."""
    seen: set[str] = set()
    kept = []
    for model in found.models:
        if model.name.startswith(BLOB_PREFIX) or model.name in seen:
            continue
        seen.add(model.name)
        kept.append(model)
    return replace(found, models=tuple(kept))


def discover(
    source: Source, *, fetch: Fetch = http_json, endpoints: Mapping[str, str] | None = None
) -> Discovery:
    """The models of `source`. Never raises; `Discovery.error` carries the reason for a failure.

    `endpoints` overrides the fixed cloud URLs (tests point it at a loopback fake).
    """
    try:
        if source.kind == "ollama":
            return _tidy(_ollama(source, fetch))
        if source.kind == "gguf":
            return _tidy(_gguf(source))
        return _tidy(_openai(source, fetch, endpoints or {}))
    except (DiscoveryError, secrets.UnsafeSecretError, OSError, ValueError) as exc:
        log.info("source %s: %s", source.name, exc)
        return Discovery(source.name, error=str(exc))

"""Prompt Guard 2 worker: scans tool output for prompt injection, one JSON line per request.

01-security "Taint, guard and reviewer". This file runs inside the guard image (or a
bubblewrap venv) with no network and read-only weights; it is not part of any importable
package, so `torch` and `transformers` never enter the environment of a `hal-*` package
(REQ-SEC-36). The host relay (`halhub.guard.serve`) starts it and speaks this protocol on its
stdin and stdout:

    {"v":1,"id":"g_1","text":"...","source_tool":"mail_read"}
    -> {"v":1,"id":"g_1","label":"injection","score":0.97,"windows":3,"ms":412,"model":"..."}
    or {"v":1,"id":"g_1","error":"..."}

Design decisions:

- Only `model.safetensors` and `tokenizer.json` are read, with `trust_remote_code` off and
  offline mode on; a model directory holding any pickle format (`.bin`, `.pt`, `.pth`,
  `.pkl`, `.ckpt`) is refused, because loading one can run code.
- Texts are split into 512-token windows with a 64-token overlap and scored in batches; the
  score is the highest malicious-class probability over the windows, so a payload in the
  middle of a long page is not averaged away.
- A model that cannot load does not stop the worker: every request gets an `error` reply,
  which the client treats as a flag (fail closed, REQ-SEC-35) and `hal guard status` shows.
- Only stdlib imports at module level, so the protocol and windowing are testable without
  `torch` (`packages/hal-hub/tests/test_guard_worker.py`).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import IO

PROTOCOL_VERSION = 1
WINDOW_TOKENS = 512  # Prompt Guard 2's context length (model card)
OVERLAP_TOKENS = 64  # 01-security "Guard": overlap between windows
BATCH_WINDOWS = 8  # windows per forward pass; bounds memory on a 2-thread CPU worker
MAX_LINE_BYTES = 1 << 20  # a request above 1 MiB is refused; the client caps texts far lower
PICKLE_SUFFIXES = frozenset({".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt"})
REQUIRED_FILES = ("model.safetensors", "tokenizer.json", "config.json")
MALICIOUS_NAMES = ("malicious", "injection", "jailbreak")

Scorer = Callable[[str], tuple[float, int]]  # text -> (highest malicious probability, windows)


def windows(ids: Sequence[int], size: int, overlap: int) -> list[Sequence[int]]:
    """`ids` cut into slices of at most `size` that overlap by `overlap`; one empty slice for none."""
    if size <= overlap:
        raise ValueError(f"window size {size} must exceed the overlap {overlap}")
    if len(ids) <= size:
        return [ids]
    step = size - overlap
    count = math.ceil((len(ids) - overlap) / step)
    return [ids[i * step : i * step + size] for i in range(count)]


def check_model_dir(path: Path) -> None:
    """Raises `ValueError` unless `path` holds the safetensors files and no pickle format."""
    if not path.is_dir():
        raise ValueError(f"model directory {path} does not exist")
    pickles = sorted(p.name for p in path.iterdir() if p.suffix.lower() in PICKLE_SUFFIXES)
    if pickles:
        raise ValueError(f"model directory {path} holds pickle files: {', '.join(pickles)}")
    missing = [name for name in REQUIRED_FILES if not (path / name).is_file()]
    if missing:
        raise ValueError(f"model directory {path} is missing {', '.join(missing)}")


def malicious_index(label2id: dict[str, int]) -> int:
    """The output index of the malicious class: a label naming it, else index 1 (binary head)."""
    for name, index in label2id.items():
        if any(word in name.lower() for word in MALICIOUS_NAMES):
            return int(index)
    return 1


def load_scorer(model_dir: Path, threads: int) -> Scorer:
    """A scorer over the Prompt Guard 2 model in `model_dir`; raises on any load problem."""
    check_model_dir(model_dir)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(threads)
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_dir, local_files_only=True, trust_remote_code=False, use_safetensors=True
    )
    model.eval()
    target = malicious_index(dict(model.config.label2id or {}))
    content = WINDOW_TOKENS - tokenizer.num_special_tokens_to_add()

    def score(text: str) -> tuple[float, int]:
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
        parts = windows(ids, content, OVERLAP_TOKENS)
        best = 0.0
        for start in range(0, len(parts), BATCH_WINDOWS):
            batch = [
                tokenizer.build_inputs_with_special_tokens(list(p))
                for p in parts[start : start + BATCH_WINDOWS]
            ]
            padded = tokenizer.pad({"input_ids": batch}, return_tensors="pt")
            with torch.no_grad():
                probs = torch.softmax(model(**padded).logits, dim=-1)[:, target]
            best = max(best, float(probs.max()))
        return best, len(parts)

    return score


def handle(line: bytes, scorer: Scorer | None, *, model: str, threshold: float, load_error: str) -> dict:
    """The reply to one request line; never raises."""
    request_id: object = None
    try:
        if len(line) > MAX_LINE_BYTES:
            raise ValueError(f"request is over {MAX_LINE_BYTES} bytes")
        request = json.loads(line)
        if not isinstance(request, dict) or request.get("v") != PROTOCOL_VERSION:
            raise ValueError(f"expected a protocol {PROTOCOL_VERSION} object")
        request_id = request.get("id")
        text = request.get("text")
        if not isinstance(request_id, str) or not isinstance(text, str):
            raise ValueError("id and text must be strings")
        if scorer is None:
            raise RuntimeError(f"model unavailable: {load_error}")
        started = time.monotonic()
        score, count = scorer(text)
        ms = round((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error reply, which flags (fail closed)
        return {"v": PROTOCOL_VERSION, "id": request_id, "error": f"{type(exc).__name__}: {exc}"}
    label = "injection" if score >= threshold else "benign"
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "label": label,
        "score": round(score, 4),
        "windows": count,
        "ms": ms,
        "model": model,
    }


def serve(lines: Iterable[bytes], out: IO[str], scorer: Scorer | None, **options: object) -> None:
    """Answers each request line in order, one JSON line each, flushing after every reply."""
    for line in lines:
        if not line.strip():
            continue
        reply = handle(line, scorer, **options)  # type: ignore[arg-type]  # options are handle's keywords
        out.write(json.dumps(reply, separators=(",", ":")) + "\n")
        out.flush()


def _stdin_lines() -> Iterator[bytes]:
    yield from sys.stdin.buffer


def main(argv: list[str] | None = None) -> int:
    """Entry point: load the model (or record why not), then serve stdin until it closes."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", type=Path, default=Path("/model"))
    parser.add_argument("--model", default="prompt-guard-2-86m")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--threads", type=int, default=2)
    opts = parser.parse_args(argv)
    scorer: Scorer | None = None
    load_error = ""
    try:
        scorer = load_scorer(opts.model_dir, opts.threads)
    except Exception as exc:  # noqa: BLE001 - reported on every request instead of exiting (see header)
        load_error = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(f"guard worker: {load_error}\n")
    serve(
        _stdin_lines(), sys.stdout, scorer, model=opts.model, threshold=opts.threshold, load_error=load_error
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

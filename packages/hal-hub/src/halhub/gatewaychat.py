"""Plugs the gateway chat client (`halkit.llm`) into the hub's classifier and reviewer.

Both take a `chat(model, messages, *, temperature, max_tokens, timeout_s)` callable so tests can
pass a fake; this module is the production one. Reason it is a separate module: the hub core
stays free of any network client, and a process without a gateway still builds a hub whose
calls simply classify as `high` (I5).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from functools import partial

from halkit import llm

from . import policy
from .classifier import ChatFn
from .reviewer import Reviewer

log = logging.getLogger(__name__)

GPU_LIGHT_TIMEOUT_S = 5  # `gpu-light status` reads a file or one socket call; longer means stuck
GPU_CHOICE_TTL_S = 10.0  # one classifier verdict per tool call must not fork gpu-light each time
LOGPROBS_TOP = 5  # alternatives per token; Guardian's yes/no mass sits in the top few
CPU_KEEP_ALIVE = "30m"  # RAM only, so holding the model costs no VRAM and spares a ~7 s reload per gap
CPU_NUM_CTX = 4096  # a classification is under 1,500 tokens; Ollama's default context wastes GBs of RAM
# Ollama options: offload no layers to the GPU; `keep_alive` the gateway sends as its top-level field
CPU_OPTIONS: dict[str, object] = {"num_gpu": 0, "num_ctx": CPU_NUM_CTX, "keep_alive": CPU_KEEP_ALIVE}

_choice: tuple[float, dict[str, object]] = (0.0, {})


def gpu_options() -> dict[str, object]:
    """Ollama options for a background chat: `{}` (GPU) or `num_gpu` 0 (CPU), by gpu-light status.

    Without gpu-light the model runs as configured. With it, the GPU is used only while the light
    is green, so the classifier never preempts a foreground job (I5). The answer is cached.
    """
    global _choice
    now = time.monotonic()
    if _choice[0] and now - _choice[0] < GPU_CHOICE_TTL_S:
        return _choice[1]
    options = _read_gpu_options()
    _choice = (now, options)
    return options


def _read_gpu_options() -> dict[str, object]:
    exe = shutil.which("gpu-light")
    if exe is None:
        return {}
    try:
        proc = subprocess.run(
            [exe, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=GPU_LIGHT_TIMEOUT_S,
            check=False,
        )
        doc = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.warning("gpu-light status failed, classifying on the CPU: %s", exc)
        return dict(CPU_OPTIONS)
    state = (doc or {}).get("effective") or (doc or {}).get("state")
    return {} if state == "green" else dict(CPU_OPTIONS)


def gateway_chat(
    model: str,
    messages: Sequence[Mapping[str, object]],
    *,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    logprobs: bool = False,
    gpu: bool = True,
) -> str | tuple[str, list[dict[str, object]]]:
    """Sends one chat request to the gateway alias `model` (GPU or CPU per `gpu_options`).

    Returns the text, or `(text, token logprobs)` when `logprobs` is true (the Guardian classifier
    reads P(yes) from them). With `gpu` false the CPU options are always sent. Raises
    `halkit.llm.GatewayError`.
    """
    options = gpu_options() if gpu else dict(CPU_OPTIONS)
    if logprobs:
        return llm.chat_logprobs(
            messages,
            model=model,
            timeout=timeout_s,
            max_tokens=max_tokens,
            temperature=temperature,
            options=options,
            top_logprobs=LOGPROBS_TOP,
            think=False,  # Granite Guardian reasons first unless told not to, and 16 tokens hold no verdict
        )
    return llm.chat(
        messages,
        model=model,
        timeout=timeout_s,
        max_tokens=max_tokens,
        temperature=temperature,
        options=options,
    )


def hub_kwargs() -> dict[str, object]:
    """`Hub(**hub_kwargs())` arguments for production: the classifier chat and, if enabled, the reviewer."""
    loaded = policy.load_policy()
    settings = loaded.reviewer
    chat: ChatFn = gateway_chat
    # `[policy].gpu = false` pins the classifier to the CPU; the reviewer keeps the gpu-light choice
    classify_chat: ChatFn = chat if loaded.classifier.gpu else partial(gateway_chat, gpu=False)
    kwargs: dict[str, object] = {"classify_chat": classify_chat}
    if settings.enabled:
        kwargs["reviewer"] = Reviewer(settings, chat)
    return kwargs

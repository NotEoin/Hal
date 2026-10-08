"""Sends one chat completion to the local model server and returns the reply text.

Speaks Ollama's /api/chat, or the OpenAI-compatible /v1/chat/completions that the
llama.cpp router serves in the max profile. HAL_CHAT_API picks which. Stdlib only.
"""
import json
import os
import time
import urllib.error
import urllib.request

CHAT_API = os.environ.get("HAL_CHAT_API", "ollama")
# Embeddings stay on Ollama in every profile, so chat has its own URL
CHAT_URL = (os.environ.get("HAL_CHAT_URL")
            or os.environ.get("HAL_OLLAMA_URL", "http://localhost:11434")).rstrip("/")


def _request(messages: list[dict], model: str, temperature: float) -> tuple[str, dict]:
    if CHAT_API == "openai":
        # Thinking costs minutes per call on the CPU modes and adds nothing to a summary
        return "/v1/chat/completions", {
            "model": model, "messages": messages, "stream": False,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    return "/api/chat", {
        "model": model, "messages": messages, "stream": False,
        "keep_alive": "30m", "options": {"temperature": temperature},
    }


def _reply_text(data: dict) -> str:
    if CHAT_API == "openai":
        return data["choices"][0]["message"].get("content") or ""
    return data.get("message", {}).get("content") or ""


def chat(messages: list[dict], model: str, *, temperature: float = 0.3,
         timeout: int = 600, retries: int = 1) -> str:
    """Returns the model's reply to `messages`, stripped.

    Retries on connection errors and timeouts with a growing pause.

    Raises:
        RuntimeError: every attempt failed.
    """
    path, payload = _request(messages, model, temperature)
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(f"{CHAT_URL}{path}", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _reply_text(json.load(r)).strip()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"model call failed after {retries + 1} tries: {last}")

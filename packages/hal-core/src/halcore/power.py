"""Turns Hal on and off, so the GPU can be handed to something else and taken back.

`hal off` stops every Hal service marked X-HalGPU or X-HalOff (voice, speech, the memory
sorter, the chat bridges, briefings) and unloads the models Ollama holds, which frees the VRAM.
Reminder daemons that use no model keep running, so phone reminders still arrive.
The llama.cpp server that `hal claude` uses for coding is not touched. `hal on` starts it
all again.

Hal holds no GPU claim while it idles: the model gateway claims the GPU light (see gpulight.py)
for each request and gives it back, so a background job is not kept off the card. `hal on` makes
no claim and does not wait for the card. `hal off` still releases a claim named `hal`, which a
machine upgraded from the old boot claim may hold.

    hal on | hal off | hal toggle | hal status        (--quiet: no desktop notification)
"""
from __future__ import annotations

import json
import subprocess
import urllib.request

from . import config, gpulight, units

NOTIFY_TIMEOUT_S = 5.0  # a missing notification daemon must not hang `hal power`
FLAG = "off"


def is_off() -> bool:
    return (config.state_dir() / FLAG).exists()


def _ollama(path: str, payload: dict | None = None, timeout: float = 10) -> dict:
    url = config.shared()["ollama_url"].rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body.splitlines()[-1]) if body.strip() else {}


def loaded_models() -> list[dict]:
    try:
        return _ollama("/api/ps").get("models", [])
    except OSError:
        return []


def unload_models() -> list[str]:
    gone = []
    for m in loaded_models():
        try:
            _ollama("/api/generate", {"model": m["name"], "keep_alive": 0}, timeout=30)
            gone.append(m["name"])
        except OSError:
            pass
    return gone


def unload_offloaded() -> list[str]:
    """Unloads the models Ollama placed partly in system RAM; returns their names.

    Ollama offloads layers to the CPU, without an error, when it loads a model into a GPU that
    is already full, and the model stays slow until it is loaded again. Unloaded, it loads
    whole on the GPU at the next request.
    """
    gone = []
    for m in loaded_models():
        if m.get("size_vram", 0) < m.get("size", 0):
            try:
                _ollama("/api/generate", {"model": m["name"], "keep_alive": 0}, timeout=30)
                gone.append(m["name"])
            except OSError:
                pass
    return gone


def _notify(title: str, body: str, quiet: bool) -> None:
    if not quiet:
        subprocess.run(["notify-send", "-a", "Hal", "-i", "audio-input-microphone", title, body], capture_output=True,
                       timeout=NOTIFY_TIMEOUT_S)


def off(quiet: bool = False) -> str:
    (config.state_dir() / FLAG).touch()
    stopped = units.off_units()
    if stopped:
        units.systemctl("stop", *stopped)
    freed = unload_models()
    # A claim named `hal` exists only on a machine upgraded from the old boot claim. Released
    # after the models are unloaded, because until then Hal still holds the VRAM.
    light = gpulight.release("hal")
    msg = (f"Stopped {len(stopped)} services and unloaded {len(freed)} models; the GPU is free. "
           "Reminders still run.")
    if light.warning:
        msg += f" Warning: {light.warning}."
    _notify("Hal is off", msg, quiet)
    return msg


def on(quiet: bool = False) -> str:
    (config.state_dir() / FLAG).unlink(missing_ok=True)
    # Models Ollama loaded partly into RAM while a background job held the card stay slow
    unload_offloaded()
    units.systemctl("start", "hal.target")
    msg = "Hal's services are starting; the voice is ready in about a minute."
    _notify("Hal is on", msg, quiet)
    return msg


def status() -> str:
    followed = units.off_units()
    active = [n for n in followed if units.systemctl("is-active", n).stdout.strip() == "active"]
    vram = sum(m.get("size_vram", 0) for m in loaded_models()) / 2**30
    state = "off" if is_off() else "on"
    lines = [f"Hal is {state}: {len(active)} of {len(followed)} services running, "
             f"{vram:.1f} GB of models loaded in Ollama."]
    lines += [f"  running: {n}" for n in active]
    light = gpulight.status_line()
    if light:
        lines.append(light)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    cmd = next((a for a in argv if not a.startswith("-")), "status")
    if cmd == "toggle":
        cmd = "on" if is_off() else "off"
    if cmd == "off":
        print(off(quiet))
    elif cmd == "on":
        print(on(quiet))
    elif cmd == "status":
        print(status())
    else:
        print("usage: hal on | off | toggle | status")
        return 2
    return 0

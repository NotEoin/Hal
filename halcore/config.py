"""Hal's shared settings: ~/.config/hal/config.toml, read fresh on every call.

The [hal] table holds what every part of Hal shares (vault, Ollama, models);
[brain] holds the agent loop's settings. HAL_CONFIG, HAL_DATA and HAL_STATE
move the config file, data and state, which is how tests run Hal in a sandbox.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # the core hal checkout
ECOSYSTEM = ROOT.parents[1]                         # hal-ecosystem, when hal sits in core/

SHARED = {
    "vault": "~/Documents/vaults/Hal",
    "ollama_url": "http://localhost:11434",
    "chat_model": "qwen3.5:9b",
    "fast_model": "qwen3.5:4b",
    "embed_model": "embeddinggemma",
}

BRAIN = {
    "model": "",                # empty = [hal].chat_model
    "keep_alive": "30m",        # how long Ollama keeps the model loaded between turns
    "history_turns": 10,        # exchanges remembered within one conversation
    "max_tool_rounds": 5,
    "tools_per_turn": 8,        # tools offered per request (plus search); the rest are found on demand
    "num_ctx": 8192,
    "temperature": 0.4,
    "user_name": "",            # how Hal addresses you; empty = no name
    "persona": "",              # empty = the built-in persona
    "deep_work": "claude",      # who does long jobs: claude (your Claude Code login) | hal (the local launcher) | off
    "deep_work_dir": "",        # where long jobs run; empty = the vault
}


def config_path() -> Path:
    return Path(os.environ.get("HAL_CONFIG", "~/.config/hal/config.toml")).expanduser()


def load() -> dict:
    """Returns the whole config file, or {} if it is missing or broken."""
    try:
        return tomllib.loads(config_path().read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def shared() -> dict:
    return {**SHARED, **load().get("hal", {})}


def brain() -> dict:
    return {**BRAIN, **load().get("brain", {})}


def state_dir() -> Path:
    path = Path(os.environ.get("HAL_STATE", "~/.local/state/hal")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def extensions_dir() -> Path:
    """Returns where extensions are cloned: [hal].extensions_dir, else ecosystem/extensions."""
    configured = shared().get("extensions_dir")
    if configured:
        return Path(configured).expanduser()
    if (ECOSYSTEM / "extensions").is_dir() or (ECOSYSTEM / "core").is_dir():
        return ECOSYSTEM / "extensions"
    return Path("~/.local/share/hal/extensions").expanduser()

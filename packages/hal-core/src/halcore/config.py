"""Hal's shared settings: ~/.config/hal/config.toml, read fresh on every call.

The [hal] table holds what every part of Hal shares (vault, Ollama, models);
[brain] holds the agent loop's settings. HAL_CONFIG, HAL_DATA and HAL_STATE
move the config file, data and state, which is how tests run Hal in a sandbox.
"""
from __future__ import annotations

import importlib
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]          # the core hal checkout: src/halcore/config.py sits in packages/hal-core/src
ECOSYSTEM = ROOT.parents[1]                         # hal-ecosystem, when hal sits in core/

def hub():
    """The `halhub` module, looked up each call because `hal eval` swaps it for a fresh one."""
    return importlib.import_module("halhub")


SHARED = {
    "vault": "~/notes",
    "ollama_url": "http://localhost:11434",
    "chat_model": "gemma4:12b-it-qat",
    "classifier_model": "granite4.1-guardian:8b",
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
    "deep_work": "off",         # long jobs: off | hal (the local launcher) | claude (explicit opt-in: not private, not sandboxed)
    "deep_work_dir": "",        # where long jobs run; empty = the vault
}


def config_path() -> Path:
    """The config file path: `HAL_CONFIG`, else `~/.config/hal/config.toml`."""
    return Path(os.environ.get("HAL_CONFIG", "~/.config/hal/config.toml")).expanduser()


def load() -> dict:
    """Returns the whole config file, or {} if it is missing or broken."""
    try:
        return tomllib.loads(config_path().read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def shared() -> dict:
    """The `[hal]` table over its defaults."""
    return {**SHARED, **load().get("hal", {})}


def brain() -> dict:
    """The `[brain]` table over its defaults."""
    return {**BRAIN, **load().get("brain", {})}


def state_path() -> Path:
    """The state folder, not created: readers use it so a dry run writes nothing."""
    return Path(os.environ.get("HAL_STATE", "~/.local/state/hal")).expanduser()


def state_dir() -> Path:
    """The state folder, created when missing."""
    path = state_path()
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

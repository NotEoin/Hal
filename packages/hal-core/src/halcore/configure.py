"""`hal configure`: writes ~/.config/hal/config.toml on first install, and keeps it pointing at this checkout.

    hal configure [--chat-model M] [--vault DIR]

A new file gets Hal's defaults and a private ntfy topic for phone notifications. An
existing file is only changed where it must be: [hub].module_paths gains this
checkout's core and extensions folders if they are missing.
"""
from __future__ import annotations

import argparse
import re
import secrets

from . import config

TEMPLATE = """# Hal's settings, shared by core hal and every extension. Each extension reads its own table;
# see each one's README for what it takes.

[hal]
vault = "{vault}"                      # the Obsidian vault Hal's memory lives in
ollama_url = "http://localhost:11434"
chat_model = "{chat_model}"            # the brain: answers and calls tools (hal eval compares models)
fast_model = "qwen3.5:4b"              # quick jobs: classifying, titling
embed_model = "embeddinggemma"         # vault search
embed_on_cpu = true                    # keeps the vault index off the GPU
vision_model = "qwen3-vl:8b"           # hal-screen

[brain]
# user_name = "Sam"                    # how Hal addresses you
# persona = \"\"\"...\"\"\"               # who Hal is; the default is a calm, precise assistant
# deep_work = "off"                    # long jobs: off | hal | claude (Claude Code: not private, not sandboxed)

[hub]
module_paths = [{paths}]
# disabled = ["phone"]                 # extensions to ignore without uninstalling

[notify]
# Phone notifications: install the ntfy app and subscribe to this topic (keep it secret).
ntfy_topic = "{topic}"
quiet_from = 22
quiet_until = 8
"""


def paths() -> list[str]:
    return [str(config.ECOSYSTEM / "core"), str(config.extensions_dir())]


def init(chat_model: str, vault: str) -> str:
    path = config.config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE.format(vault=vault, chat_model=chat_model, topic=f"hal-{secrets.token_hex(12)}",
                                        paths=", ".join(f'"{p}"' for p in paths())))
        path.chmod(0o600)
        return f"wrote {path}"
    text = path.read_text()
    current = config.load().get("hub", {}).get("module_paths")
    if current is None:
        line = "module_paths = [" + ", ".join(f'"{p}"' for p in paths()) + "]"
        if re.search(r"^\[hub\]\s*$", text, re.M):
            text = re.sub(r"^\[hub\]\s*$", "[hub]\n" + line, text, count=1, flags=re.M)
        else:
            text += f"\n[hub]\n{line}\n"
    else:
        from pathlib import Path
        have = {str(Path(p).expanduser().resolve()) for p in current}
        missing = [p for p in paths() if str(Path(p).resolve()) not in have]
        if not missing:
            return f"{path} is up to date"
        new = current + missing
        text = re.sub(r"^module_paths\s*=.*$", "module_paths = [" + ", ".join(f'"{p}"' for p in new) + "]",
                      text, count=1, flags=re.M)
    path.write_text(text)
    return f"updated [hub].module_paths in {path}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hal configure")
    ap.add_argument("--chat-model", default=config.SHARED["chat_model"])
    ap.add_argument("--vault", default=config.SHARED["vault"])
    args = ap.parse_args(argv)
    print(init(args.chat_model, args.vault))
    return 0

"""`hal configure`: writes ~/.config/hal/config.toml on first install, and keeps it pointing at this checkout.

    hal configure [--chat-model M] [--vault DIR] [--migrate [HAL_CONF]] [--yes]

A new file gets Hal's defaults and a private ntfy topic for phone notifications. An
existing file is only changed where it must be: [hub].module_paths gains this
checkout's core and extensions folders if they are missing.

Models are not chosen here. After the file is written, `hal configure` runs the same engine as
`hal tune --auto` and prints the plan; `hal tune` sets everything about the models. Nothing is
downloaded.
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

from . import config, confmigrate

TEMPLATE = """# Hal's settings, shared by core hal and every extension. Each extension reads its own table;
# see each one's README for what it takes.

[hal]
vault = "{vault}"                      # the Obsidian vault Hal's memory lives in
ollama_url = "http://localhost:11434"
{chat_line}embed_on_cpu = true                    # keeps the vault index off the GPU
# Models: point Hal at the ones you have with `hal models add-source ...`, then `hal tune --auto --write`.

[models]
kv_type = "auto"                       # default KV cache type: auto, f16, q8_0 or q4_0

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
    """The folders searched for modules."""
    return [str(config.ECOSYSTEM / "core"), str(config.extensions_dir())]


def init(chat_model: str, vault: str) -> str:
    """Creates or updates the config file; `chat_model` is written only when not empty."""
    path = config.config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        chat_line = f'chat_model = "{chat_model}"\n' if chat_model else ""
        path.write_text(TEMPLATE.format(vault=vault, chat_line=chat_line, topic=f"hal-{secrets.token_hex(12)}",
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
        have = {str(Path(p).expanduser().resolve()) for p in current}
        missing = [p for p in paths() if str(Path(p).resolve()) not in have]
        if not missing:
            return f"{path} is up to date"
        new = current + missing
        text = re.sub(r"^module_paths\s*=.*$", "module_paths = [" + ", ".join(f'"{p}"' for p in new) + "]",
                      text, count=1, flags=re.M)
    path.write_text(text)
    return f"updated [hub].module_paths in {path}"


def show_plan() -> None:
    """Runs the `hal tune --auto` engine without writing and prints the plan and the next step."""
    from . import tune  # lazy: it pulls in the gateway package

    print()
    status = tune.main([])
    if status not in (tune.EXIT_OK, tune.EXIT_NOTHING):
        print("hal configure: the model plan could not be made; run `hal tune --auto` to see why", file=sys.stderr)


def migrate(conf_arg: str, yes: bool) -> int:
    """Shows the diff for an old `hal.conf` and writes it after a typed y (REQ-CLI-84)."""
    conf_path = Path(conf_arg) if conf_arg else config.ECOSYSTEM / "core" / "hal" / "hal.conf"
    try:
        conf = confmigrate.parse_conf(conf_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        print(f"hal configure: cannot read {conf_path}: {exc}", file=sys.stderr)
        return 1
    target = config.config_path()
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    new, notes = confmigrate.migrate_text(old, conf, str(conf_path))
    for note in notes:
        print(f"note: {note}")
    if new == old:
        print("nothing to change")
        return 0
    print(confmigrate.diff(old, new, str(target)), end="")
    if not yes:
        if not sys.stdin.isatty():
            print("hal configure: --migrate needs a terminal to confirm, or --yes", file=sys.stderr)
            return 2
        if input("Write these changes? [y/N] ").strip().lower() != "y":
            print("nothing written")
            return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = confmigrate.apply(target, new)
    print(f"wrote {target}" + (f" (backup {backup.name})" if old else ""))
    return 0


def main(argv: list[str]) -> int:
    """Entry point of `hal configure`."""
    ap = argparse.ArgumentParser(prog="hal configure")
    ap.add_argument("--chat-model", default=None)
    ap.add_argument("--vault", default=config.SHARED["vault"])
    ap.add_argument("--migrate", nargs="?", const="", metavar="HAL_CONF", help="add keys from an old hal.conf")
    ap.add_argument("--yes", action="store_true", help="kept for scripts; configure never asks any more")
    args = ap.parse_args(argv)
    if args.migrate is not None:
        return migrate(args.migrate, args.yes)
    print(init(args.chat_model or "", args.vault))
    show_plan()
    return 0

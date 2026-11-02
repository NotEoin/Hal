"""`hal models`: list model sources and their models, add or remove a source, assign a role.

    hal models [list] [--source NAME] [--json]
    hal models sources [--json]
    hal models add-source KIND LOCATION [--name NAME] [--key-file FILE] [--allow-remote]
    hal models remove-source NAME
    hal models assign ROLE SOURCE MODEL

`LOCATION` is a URL for `ollama` and `openai`, a folder for `gguf`; `anthropic` and
`openai-cloud` take no location and need `--key-file`. `assign` names the model for a role,
pins it and wires the gateway, the same as `hal tune --write --role ROLE` for that model. No
command here downloads a model.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from halkit import hardware, modelconf, paths, tomledit

from . import confmigrate, modelplan

EXIT_OK, EXIT_ERROR = 0, 1
KINDS_WITH_URL = modelconf.URL_KINDS
NO_LOCATION = modelconf.KEY_FILE_KINDS


def _read() -> str:
    target = paths.config_file()
    return target.read_text(encoding="utf-8") if target.exists() else ""


def _validated_write(new: str) -> None:
    """Checks `new` with the real loaders on a scratch copy, then writes it with a backup."""
    with tempfile.TemporaryDirectory(prefix="hal-models-") as scratch:
        probe = Path(scratch) / "config.toml"
        probe.write_text(new, encoding="utf-8")
        modelconf.load_roles(probe)
    confmigrate.apply(paths.config_file(), new)


def source_name(kind: str, taken: set[str], wanted: str | None) -> str:
    """`wanted`, else the kind, with `-2`, `-3` ... added until it is free."""
    if wanted:
        return wanted
    name, n = kind, 2
    while name in taken:
        name, n = f"{kind}-{n}", n + 1
    return name


def add_source(args: argparse.Namespace) -> int:
    """`hal models add-source`."""
    text = _read()
    taken = set(modelconf.load_sources())
    name = source_name(args.kind, taken, args.name)
    if name in taken:
        print(f"hal models: source {name!r} exists; remove it first or pick --name", file=sys.stderr)
        return EXIT_ERROR
    values: dict[str, object] = {"kind": args.kind}
    if args.kind in KINDS_WITH_URL:
        values["url"] = args.location or modelconf.DEFAULT_URLS[args.kind]
    elif args.kind == "gguf":
        if not args.location:
            print("hal models: a gguf source needs a folder: hal models add-source gguf ~/models", file=sys.stderr)
            return EXIT_ERROR
        values["path"] = args.location
    elif args.location:
        print(f"hal models: a {args.kind} source takes no location; use --key-file", file=sys.stderr)
        return EXIT_ERROR
    if args.key_file:
        values["api_key_file"] = args.key_file
    if args.allow_remote:
        values["allow_remote"] = True
    try:
        _validated_write(tomledit.set_table(text, f"models.sources.{name}", values))
    except (ValueError, OSError, *modelplan.plain_errors()) as exc:
        print(f"hal models: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"added source {name}; next: hal models list")
    return EXIT_OK


def remove_source(args: argparse.Namespace) -> int:
    """`hal models remove-source`; refused while a role uses the source."""
    sources = modelconf.load_sources()
    if args.name not in sources:
        print(f"hal models: no source named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    users = [r.role for r in modelconf.load_roles().values() if r.source == args.name]
    if users:
        print(f"hal models: roles {', '.join(users)} use {args.name!r}; assign them elsewhere first", file=sys.stderr)
        return EXIT_ERROR
    _validated_write(tomledit.remove_table(_read(), f"models.sources.{args.name}"))
    print(f"removed source {args.name}")
    return EXIT_OK


def list_models(args: argparse.Namespace) -> int:
    """`hal models list` and `hal models sources`."""
    sources = modelconf.load_sources()
    if args.source:
        sources = {n: s for n, s in sources.items() if n == args.source}
        if not sources:
            print(f"hal models: no source named {args.source!r}", file=sys.stderr)
            return EXIT_ERROR
    only_sources = getattr(args, "command", "list") == "sources"
    found = {} if only_sources else modelplan.survey(sources)
    if args.json:
        doc = {n: {"kind": s.kind, "url": s.url, "path": s.path, "models": [m.to_dict() for m in found[n].models] if n in found else [],
                   "error": found[n].error if n in found else ""} for n, s in sources.items()}
        print(json.dumps(doc, indent=2))
        return EXIT_OK
    if not sources:
        print("No model sources yet. Add one, for example:\n  hal models add-source ollama http://127.0.0.1:11434")
        return EXIT_OK
    for name, src in sources.items():
        where = src.url or src.path or "(key file)"
        print(f"{name}  [{src.kind}]  {where}")
        if only_sources:
            continue
        d = found[name]
        if d.error:
            print(f"    unreachable: {d.error}")
        for m in d.models:
            size = f"{m.size_bytes / 1024**3:.1f} GiB" if m.size_bytes else "size ?"
            bits = [size, f"{m.params_b:g}B" if m.params_b else "", m.quant, f"ctx {m.context_native}" if m.context_native else "",
                    ",".join(sorted(m.capabilities))]
            print(f"    {m.name}  " + "  ".join(b for b in bits if b))
        for note in d.notes:
            print(f"    note: {note}")
    return EXIT_OK


def assign(args: argparse.Namespace) -> int:
    """`hal models assign ROLE SOURCE MODEL`: set the role, then pin and wire it."""
    text = _read()
    try:
        sources = modelconf.load_sources()
        if args.source not in sources:
            raise ValueError(f"no source named {args.source!r}; hal models add-source ...")
        found = modelplan.survey({args.source: sources[args.source]})
        if modelplan._find(found, args.source, args.model) is None:  # noqa: SLF001 - same package
            reason = found[args.source].error or "it is not in the source's model list"
            raise ValueError(f"{args.model!r} not available from {args.source!r}: {reason}")
        _validated_write(tomledit.set_table(text, f"models.roles.{args.role}", {"source": args.source, "model": args.model}))
        roles = modelconf.load_roles()
        plans = modelplan.build(roles, sources, found, hardware.detect(), modelconf.limits(), only=(args.role,))
        for line in modelplan.apply(plans, found):
            print(line)
    except (ValueError, OSError, *modelplan.plain_errors()) as exc:
        print(f"hal models: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def main(argv: list[str]) -> int:
    """Entry point of `hal models`."""
    ap = argparse.ArgumentParser(prog="hal models", description="Model sources and the models in them.")
    sub = ap.add_subparsers(dest="command")
    for name in ("list", "sources"):
        p = sub.add_parser(name, help="list sources" + (" and their models" if name == "list" else " only"))
        p.add_argument("--source", help="only this source")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("add-source", help="add a model source")
    p.add_argument("kind", choices=modelconf.SOURCE_KINDS)
    p.add_argument("location", nargs="?", help="URL (ollama, openai) or folder (gguf)")
    p.add_argument("--name", help="name of the source (default: the kind)")
    p.add_argument("--key-file", help="file that holds the API key (never the key itself)")
    p.add_argument("--allow-remote", action="store_true", help="allow a non-loopback https URL")
    p = sub.add_parser("remove-source", help="remove a source no role uses")
    p.add_argument("name")
    p = sub.add_parser("assign", help="give a role a model, pin it and wire the gateway")
    p.add_argument("role", choices=modelconf.ROLES)
    p.add_argument("source")
    p.add_argument("model")
    args = ap.parse_args(argv)
    if args.command is None:
        args = ap.parse_args(["list", *argv])
    try:
        handler = {"list": list_models, "sources": list_models, "add-source": add_source,
                   "remove-source": remove_source, "assign": assign}[args.command]
        return handler(args)
    except modelplan.plain_errors() as exc:
        print(f"hal models: {exc}", file=sys.stderr)
        return EXIT_ERROR

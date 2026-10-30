"""`hal code [DIR]`: the secure coding path, hal-agent in a docker box (see halsandbox.code).

    hal code [DIR] [--upstream URL] [--model NAME] [--image NAME] [--runtime runc|runsc]
             [-- AGENT ARGS...]
    hal code --build
    hal code ls | accept SID [--allow-flags] | export SID FILE [--bundle] | reject SID

By default the box works on an isolated copy of DIR; `accept` or `export` is how changes leave it.
`--direct` mounts DIR itself and needs `[sandbox].lockdown` of relaxed or lower.

Without `--build` it needs the agent image; `--build` creates it from this checkout. The model
server and model default to Hal's own `[hal] ollama_url` and `chat_model`, so `hal code` uses
the same local model as `hal chat`; HAL_CODE_UPSTREAM and HAL_AGENT_MODEL override them.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from halsandbox import code as sandbox
from halsandbox import isolated

from . import config

REVIEW_COMMANDS = ("ls", "accept", "export", "reject")
ROOT = Path(__file__).resolve().parents[4]  # packages/hal-core/src/halcore -> checkout root


def review(argv: list[str]) -> int:
    """Runs `hal code ls|accept|export|reject`; returns the exit status."""
    p = argparse.ArgumentParser(prog="hal code")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    acc = sub.add_parser("accept")
    acc.add_argument("sid")
    acc.add_argument("--allow-flags", action="store_true", help="apply even high-risk changes")
    exp = sub.add_parser("export")
    exp.add_argument("sid")
    exp.add_argument("file")
    exp.add_argument("--bundle", action="store_true", help="write a git bundle instead of a patch")
    rej = sub.add_parser("reject")
    rej.add_argument("sid")
    opts = p.parse_args(argv)
    try:
        if opts.cmd == "ls":
            for sid in isolated.sessions():
                print(f"{sid}  {isolated.load(sid).project}")
            return 0
        iso = isolated.load(opts.sid)
        if opts.cmd == "accept":
            report = isolated.accept(iso, allow_flags=opts.allow_flags)
            print("\n".join(f"{c.status} {c.path}{'  [' + c.flag + ']' if c.flag else ''}" for c in report) or "no changes")
        elif opts.cmd == "export":
            print(isolated.export(iso, Path(opts.file), "bundle" if opts.bundle else "patch"))
        else:
            isolated.reject(iso)
    except sandbox.SandboxError as exc:
        print(f"hal code {opts.cmd}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    """Runs `hal code`; returns the exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if argv and argv[0] in REVIEW_COMMANDS:
        return review(argv)
    if "--" in argv:
        cut = argv.index("--")
        argv, agent_args = argv[:cut], argv[cut + 1 :]
    else:
        agent_args = []
    p = argparse.ArgumentParser(prog="hal code", description="run hal-agent in a docker box")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--build", action="store_true", help="build the agent image and exit")
    shared = config.shared()
    p.add_argument("--upstream", default=os.environ.get("HAL_CODE_UPSTREAM", shared["ollama_url"]))
    p.add_argument("--model", default=os.environ.get("HAL_AGENT_MODEL", shared["chat_model"]))
    p.add_argument("--image", default=sandbox.DEFAULT_IMAGE)
    p.add_argument("--runtime", choices=("runc", "runsc"))
    p.add_argument("--direct", action="store_true", help="mount DIR itself (lockdown relaxed or lower)")
    opts = p.parse_args(argv)
    if "--model" not in agent_args:
        agent_args = ["--model", opts.model, *agent_args]
    try:
        if opts.build:
            sandbox.build_image(ROOT, opts.image)
            return 0
        return sandbox.run_code(
            Path(opts.dir), upstream=opts.upstream, image=opts.image, runtime=opts.runtime, direct=opts.direct, agent_args=agent_args
        )
    except (sandbox.SandboxError, ValueError, OSError) as exc:
        print(f"hal code: {exc}", file=sys.stderr)
        return 1

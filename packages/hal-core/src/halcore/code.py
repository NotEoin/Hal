"""`hal code [DIR]`: the secure coding path, hal-agent in a docker box (see halsandbox.code).

    hal code [DIR] [--upstream URL] [--model NAME] [--image NAME] [--runtime runc|runsc]
             [-- AGENT ARGS...]
    hal code --build
    hal code ls | accept SID [--allow-flags] | export SID FILE [--bundle] [--force] | reject SID

On a terminal it runs the TUI over ACP (streaming, slash commands, /rewind, /diff, /apply); with `-- AGENT ARGS`
or without a terminal it runs the agent's own prompt in the box. By default the box works on an isolated copy of DIR; `accept` or `export` is how changes leave it.
`--direct` mounts DIR itself and needs `[sandbox].lockdown` of relaxed or lower.

Without `--build` it needs the agent image; `--build` creates it from this checkout. The box
asks the gateway for `hal-code`, the code role with the context `hal tune` set, through a
session that cannot reach a cloud backend (`halcore.modellink`). Without a running gateway it
uses `[hal] code_model` (else `chat_model`) at `ollama_url`. `--upstream` (HAL_CODE_UPSTREAM)
and `--model` (HAL_AGENT_MODEL) override them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from halsandbox import code as sandbox
from halsandbox import isolated

from . import modellink, sessions
from .tui.app import run_tui

EXIT_REFUSED = 3  # 06-core-cli exit code table
REVIEW_COMMANDS = ("ls", "diff", "accept", "export", "reject")
log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[4]  # packages/hal-core/src/halcore -> checkout root


def review(argv: list[str]) -> int:
    """Runs `hal code ls|accept|export|reject`; returns the exit status."""
    p = argparse.ArgumentParser(prog="hal code")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    dif = sub.add_parser("diff", help="show the patch, stat and risk flags without applying")
    dif.add_argument("sid")
    acc = sub.add_parser("accept")
    acc.add_argument("sid")
    acc.add_argument("--allow-flags", action="store_true", help="apply even high-risk changes")
    exp = sub.add_parser("export")
    exp.add_argument("sid")
    exp.add_argument("file")
    exp.add_argument("--bundle", action="store_true", help="write a git bundle instead of a patch")
    exp.add_argument("--force", action="store_true", help="overwrite FILE if it exists")
    rej = sub.add_parser("reject")
    rej.add_argument("sid")
    opts = p.parse_args(argv)
    try:
        if opts.cmd == "ls":
            for sid in isolated.sessions():
                print(f"{sid}  {isolated.load(sid).project}")
            return 0
        iso = isolated.load(opts.sid)
        if opts.cmd == "diff":
            rep = isolated.report(iso)
            print(rep.patch)
            print(isolated.render(rep))
        elif opts.cmd == "accept":
            print(isolated.render(isolated.report(iso)))
            applied = isolated.accept(iso, allow_flags=opts.allow_flags)
            print(f"applied {len(applied)} changed paths" if applied else "no changes")
        elif opts.cmd == "export":
            print(isolated.export(iso, Path(opts.file), "bundle" if opts.bundle else "patch", force=opts.force))
        else:
            isolated.reject(iso)
    except sandbox.SandboxError as exc:
        print(f"hal code {opts.cmd}: {exc}", file=sys.stderr)
        return 1
    return 0


async def _tui(opts: argparse.Namespace) -> int:
    """Runs the TUI over ACP in a coding box; the real project stays unmounted unless `--direct`."""
    project = Path(opts.dir).resolve()
    box = await sessions.BoxedAgent.start(
        kind="code", project=project, channel="cli", upstream=opts.upstream or None, model=opts.model or None,
        image=opts.image, runtime=opts.runtime, direct=opts.direct,
    )  # fmt: skip
    try:
        return await run_tui(box, cwd=sessions.BOX_WORK, project=project, model=box.link.route.model, resume=opts.resume)
    finally:
        await box.close()
        report_changes(box.session.iso)


def report_changes(iso: isolated.Isolated | None) -> None:
    """Logs how to apply the copy's changes, only when it has some (a clean exit says nothing)."""
    if iso is None:
        return
    try:
        count = len(isolated.changes(iso))
    except (sandbox.SandboxError, OSError) as exc:
        log.warning("cannot list the changes of isolated session %s: %s", iso.sid, exc)
        return
    if count:
        noun = "change" if count == 1 else "changes"
        log.warning("%d unaccepted %s in isolated session %s; `hal code accept %s` applies them", count, noun, iso.sid, iso.sid)


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
    p.add_argument("--upstream", default=os.environ.get(modellink.UPSTREAM_ENV, ""),
                   help="a model server URL to use instead of the gateway")  # fmt: skip
    p.add_argument("--model", default=os.environ.get(modellink.MODEL_ENV, ""), help="default: hal-code")
    p.add_argument("--image", default=sandbox.DEFAULT_IMAGE)
    p.add_argument("--runtime", choices=("runc", "runsc"))
    p.add_argument("--direct", action="store_true", help="mount DIR itself (lockdown relaxed or lower)")
    p.add_argument("--resume", metavar="SID", default="", help="reopen stored session SID (see hal sessions)")
    opts = p.parse_args(argv)
    interactive = os.isatty(0) and os.isatty(1)
    headless = opts.build or not interactive or "--" in sys.argv
    try:
        if opts.resume and not interactive:
            print("hal code: --resume needs a terminal", file=sys.stderr)
            return EXIT_REFUSED
        if not headless:
            return asyncio.run(_tui(opts))
        if opts.build:
            sandbox.build_image(ROOT, opts.image)
            return 0
        found = modellink.route("code", upstream=opts.upstream, model=opts.model)
        if "--model" not in agent_args:
            agent_args = ["--model", found.model, *agent_args]
        return sandbox.run_code(
            Path(opts.dir), image=opts.image, runtime=opts.runtime, direct=opts.direct, agent_args=agent_args,
            serve_model=lambda sock: modellink.open(found, sock, "code box").close,
        )  # fmt: skip
    except sandbox.SandboxError as exc:
        print(f"hal code: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (ValueError, OSError) as exc:
        print(f"hal code: {exc}", file=sys.stderr)
        return 1

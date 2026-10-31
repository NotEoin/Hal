"""Command line for `hal box` and `hal-sandbox` (02-sandbox "hal box", step 32).

`box_main` parses the `hal box` options before `--` and passes everything after it to the CLI
unchanged. `sandbox_main` is the `hal-sandbox` script: `harnesses`, `image`, `deps`, `doctor`, `ls`,
`gc`, `forget`.
Entry points print; the library modules log.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from halkit.termsafe import strip_terminal_controls

from . import box as boxmod
from . import boximages, images, labels, projects
from . import deps as depsmod
from . import doctor as doctormod
from .errors import SandboxError
from .forwarder import ModelForwarder

log = logging.getLogger(__name__)

EX_USAGE = 64
EX_FAIL = 1


class _Forwarder:
    """Adapts `ModelForwarder` to the `Closeable` that `box.run` expects."""

    def __init__(self, socket: Path, upstream: str) -> None:
        self.server = ModelForwarder(socket, upstream)
        self.server.start()

    def close(self) -> None:
        """Stops the forwarder."""
        self.server.stop()


def _out(text: object) -> str:
    return strip_terminal_controls(str(text))


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2))


def _split(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    args = list(argv)
    if "--" in args:
        i = args.index("--")
        return args[:i], args[i + 1 :]
    return args, []


def _harnesses(as_json: bool) -> int:
    rows = boximages.harnesses()
    if as_json:
        _print_json(rows)
    else:
        for r in rows:
            print(
                f"{r['name']:9} {r['version']:10} {'built ' + str(r['image']) if r['built'] else 'not built'}"
            )
    return 0


def _image(args: argparse.Namespace) -> int:
    if args.action == "status":
        rows = images.status()
        if args.json:
            _print_json(rows)
        else:
            for r in rows:
                print(_out(f"{r['tag']}  {r['id'][:19]}  {int(r['size']) // 10**6} MB  {r['built']}"))
        return 0
    names = (*images.FLAVORS, *boximages.CLIS) if args.name == "all" else (args.name,)
    done = []
    for name in names:
        if name in images.FLAVORS:
            info = images.build(name, no_cache=args.no_cache)
            done.append({"name": name, "tag": info.tag})
        elif name in boximages.CLIS:
            base = images.status()
            if not base:
                raise SandboxError("build the sandbox image first: `hal box image build`")
            done.append({"name": name, "tag": boximages.build_box(name, base_tag=base[0]["tag"])})
        else:
            raise SandboxError(f"unknown image {name!r}; choose sandbox, sandbox-slim, a CLI or all")
    if args.json:
        _print_json(done)
    else:
        for d in done:
            print(f"built {d['tag']}")
    return 0


def _ask(lockfile: depsmod.Lockfile) -> bool:
    if not sys.stdin.isatty():
        return False  # no terminal, no approval
    reply = input(
        _out(f"Install dependencies from {lockfile.path} ({lockfile.manager}) in the prep box? [y/N] ")
    )
    return reply.strip().lower() in ("y", "yes")


def _deps(args: argparse.Namespace) -> int:
    results = depsmod.prepare(Path(args.dir), approve=_ask)
    if args.json:
        _print_json([{"lockfile": r.lockfile.path, "ok": r.ok, "exit": r.exit_code} for r in results])
    else:
        print(f"prepared {len(results)} lockfile(s)")
    return 0 if all(r.ok for r in results) else EX_FAIL


def _doctor(args: argparse.Namespace) -> int:
    checks = doctormod.run_checks(refresh=args.refresh)
    if args.json:
        _print_json(doctormod.as_dicts(checks))
    else:
        for c in checks:
            print(_out(f"{c.status:4} {c.name}: {c.detail}"))
    return EX_FAIL if any(c.status == doctormod.FAIL for c in checks) else 0


def _ls(args: argparse.Namespace) -> int:
    rows = doctormod.list_boxes()
    if args.json:
        _print_json(rows)
    else:
        for r in rows:
            print(_out(f"{r['name']}  {r['kind']}  {r['session']}  {r['status']}"))
    return 0


def _gc(args: argparse.Namespace) -> int:
    res = labels.sweep(dry_run=args.dry_run)
    if res.error:
        print(_out(f"hal-sandbox: {res.error}"), file=sys.stderr)
        return EX_FAIL
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {len(res.removed)} container(s), {len(res.dirs_removed)} directorie(s)")
    return 0


def _forget(args: argparse.Namespace) -> int:
    print("forgot" if projects.forget(Path(args.dir).resolve()) else "nothing to forget")
    return 0


_SIMPLE = {"doctor": _doctor, "ls": _ls, "gc": _gc, "forget": _forget}


def sandbox_main(argv: Sequence[str] | None = None) -> int:
    """The `hal-sandbox` script; returns the exit code."""
    parser = argparse.ArgumentParser(prog="hal-sandbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harnesses", help="list the supported agent CLIs")
    h.add_argument("--json", action="store_true")
    img = sub.add_parser("image", help="build or list images")
    img.add_argument("action", choices=("build", "status"))
    img.add_argument("name", nargs="?", default="sandbox")
    img.add_argument("--no-cache", action="store_true")
    img.add_argument("--json", action="store_true")
    d = sub.add_parser("deps", help="install project dependencies in the egress-limited prep box")
    d.add_argument("--dir", default=".")
    d.add_argument("--json", action="store_true")
    dr = sub.add_parser("doctor", help="check lockdown, backend, runtime and images")
    dr.add_argument("--refresh", action="store_true", help="ignore the cached probe")
    dr.add_argument("--json", action="store_true")
    ls = sub.add_parser("ls", help="list running boxes")
    ls.add_argument("--json", action="store_true")
    gc = sub.add_parser("gc", help="remove orphaned boxes and empty session directories")
    gc.add_argument("--dry-run", action="store_true")
    fg = sub.add_parser("forget", help="drop the saved choices for a project")
    fg.add_argument("dir", nargs="?", default=".")
    args = parser.parse_args(argv)
    try:
        if args.cmd in _SIMPLE:
            return _SIMPLE[args.cmd](args)
        if args.cmd == "harnesses":
            return _harnesses(args.json)
        if args.cmd == "image":
            return _image(args)
        return _deps(args)
    except SandboxError as exc:
        print(_out(f"hal-sandbox: {exc}"), file=sys.stderr)
        return EX_FAIL


def box_main(argv: Sequence[str] | None = None) -> int:
    """`hal box [options] -- CLI [CLI-ARGS...]`, `hal box harnesses`, `hal box image ...`."""
    own, rest = _split(sys.argv[1:] if argv is None else argv)
    if own[:1] == ["harnesses"]:
        return sandbox_main(["harnesses", *own[1:]])
    if own[:1] and own[0] in ("image", "doctor", "ls", "gc", "forget"):
        return sandbox_main(own)
    parser = argparse.ArgumentParser(prog="hal box")
    parser.add_argument("--dir", default=".")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--direct", action="store_true", help="mount the real project (lockdown relaxed)")
    parser.add_argument("--image")
    parser.add_argument("--model", default=boxmod.DEFAULT_MODEL)
    parser.add_argument("--ask", action="store_true", help="keep the CLI's own prompts")
    parser.add_argument("--no-checkpoint", action="store_true")
    parser.add_argument("--upstream", default="http://127.0.0.1:8080")
    parser.add_argument("cli", nargs="?")
    args = parser.parse_args(own)
    if not args.cli and rest:
        args.cli, rest = (
            rest[0],
            rest[1:],
        )  # `hal box -- CLI ARGS`: the CLI is the first word after `--` (REQ-CLI-42)
    if not args.cli:
        parser.error("name a CLI after `--`: hal box -- claude")
    try:
        return boxmod.run(
            args.cli, rest, project=Path(args.dir), backend=args.backend, direct=args.direct,
            image=args.image, model=args.model, ask=args.ask,
            checkpoints=not args.no_checkpoint,
            start_model=lambda sock, _sid: _Forwarder(sock, args.upstream),
        )  # fmt: skip
    except SandboxError as exc:
        print(_out(f"hal box: {exc}"), file=sys.stderr)
        return EX_FAIL


main = box_main  # `hal box` is dispatched to `halsandbox.cli.main` by halcore


if __name__ == "__main__":
    raise SystemExit(sandbox_main())

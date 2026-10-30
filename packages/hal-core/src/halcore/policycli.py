"""`hal policy` and `hal audit`: look at, check, edit and extend the tool policy and read its log.

01-security owns the policy engine and the audit log; this module only names the commands and
prints their results. Design decisions:

- `explain` asks the hub for a decision and never runs a tool (REQ-CLI-70), so it is safe to run
  against any tool and any arguments.
- `edit` and `allow` never write `policy.toml` until the new text loads (REQ-CLI-71, 102); the old
  file is kept as `policy.toml.bak`. `allow` needs a `y` typed on a terminal, so a script or a
  model piping input cannot extend the policy (01 SD2).
- Everything printed from the log or from a tool's text goes through `halkit.termsafe.escape`,
  because both can carry terminal control characters from a hostile tool.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from difflib import unified_diff
from pathlib import Path

from halhub import audit
from halhub.hub import Hub
from halhub.policy import PROFILES, ConfigError, check_rules, edit, load_policy
from halhub.policy import explain as explain_decision
from halkit import files, paths
from halkit.termsafe import escape

EXIT_USAGE = 2
EXIT_ASK = 20  # `hal policy explain --exit-code`: the call would ask
EXIT_DENY = 21  # ... or be denied (06 exit codes)
TAIL_DEFAULT = 20
EDITOR_TIMEOUT_S = 3600  # the person is typing; an hour-long session ends the edit, nothing is installed
YES_ANSWERS = frozenset({"y", "yes"})
STARTER = """\
# Hal tool policy. Documentation: docs/howto/approve-tool-calls.md
# `hal policy check` validates this file; `hal policy explain TOOL` shows one decision.
schema_version = 1
profile = "default"
"""


def main(argv: Sequence[str]) -> int:
    """Runs `policy` or `audit` subcommands (`argv[0]` is the group); returns the exit status."""
    if not argv or argv[0] not in ("policy", "audit"):
        print("usage: hal policy ... | hal audit ...", file=sys.stderr)
        return EXIT_USAGE
    group, rest = argv[0], list(argv[1:])
    try:
        return (_policy if group == "policy" else _audit)(rest)
    except (ConfigError, edit.PolicyEditError, ValueError, OSError) as exc:
        print(f"hal {group}: {escape(str(exc))}", file=sys.stderr)
        return 1


def _parser(prog: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"hal {prog}")


def _policy(argv: list[str]) -> int:
    parser = _parser("policy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show")
    check = sub.add_parser("check")
    check.add_argument("file", nargs="?")
    explain = sub.add_parser("explain")
    explain.add_argument("tool")
    explain.add_argument("--args", default="{}")
    explain.add_argument("--exit-code", action="store_true")
    init = sub.add_parser("init")
    init.add_argument("--force", action="store_true")
    sub.add_parser("edit")
    allow = sub.add_parser("allow")
    allow.add_argument("tool")
    allow.add_argument("--args")
    allow.add_argument("--domain")
    for each in (show, explain):
        each.add_argument("--profile", choices=sorted(PROFILES))
        each.add_argument("--channel", choices=("voice", "telegram", "remote"))
    show.add_argument("--json", action="store_true")
    opts = parser.parse_args(argv)
    if opts.cmd == "show":
        return _show(opts)
    if opts.cmd == "check":
        return _check(Path(opts.file) if opts.file else paths.policy_file())
    if opts.cmd == "explain":
        return _explain(opts)
    if opts.cmd == "init":
        return _init(force=opts.force)
    if opts.cmd == "edit":
        return _edit()
    return _allow(opts)


def _show(opts: argparse.Namespace) -> int:
    loaded = load_policy(profile=opts.profile, channel=opts.channel)
    data = {
        "file": str(loaded.path) if loaded.path else None,
        "profile": loaded.profile.name,
        "channel": loaded.channel_profile.name if loaded.channel_profile else None,
        "tool_rules": sorted(loaded.tools),
        "pack_rules": sorted(loaded.packs),
        "approval_timeout_s": loaded.approval.timeout_s,
    }
    if opts.json:
        print(json.dumps(data, indent=2))
        return 0
    for key, value in data.items():
        print(f"{key}: {escape(str(value))}")
    return 0


def _check(path: Path) -> int:
    loaded = load_policy(path)
    hub = Hub()
    session = hub.open_session(kind="hub", origin="cli")
    check_rules(loaded, [hub.tool_meta(tool) for tool in hub.tools(session)])
    print(f"{escape(str(path))}: ok (profile {loaded.profile.name})")
    return 0


def _explain(opts: argparse.Namespace) -> int:
    args = json.loads(opts.args)
    if not isinstance(args, dict):
        raise ValueError("--args must be a JSON object")
    hub = Hub()
    session = hub.open_session(kind="hub", origin="cli", profile=opts.profile, channel=opts.channel)
    decision = hub.explain(opts.tool, args, session)
    if decision is None:
        print(f"hal policy: no tool named {escape(opts.tool)}", file=sys.stderr)
        return EXIT_USAGE
    print(_safe(explain_decision(decision)))
    if not opts.exit_code:
        return 0
    return {"ask": EXIT_ASK, "deny": EXIT_DENY}.get(decision.verdict.value, 0)


def _init(*, force: bool) -> int:
    path = paths.policy_file()
    if path.exists() and not force:
        raise ValueError(f"{path} exists; use --force to replace it")
    _install(path, STARTER)
    print(f"wrote {path}")
    return 0


def _safe(text: str) -> str:
    """`text` with control characters escaped on every line, keeping the line breaks."""
    return "\n".join(escape(line) for line in text.splitlines())


def _install(path: Path, text: str) -> None:
    """Replaces `path` with `text` (mode 0600), keeping the previous file as `<path>.bak`."""
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    files.write_atomic(path, text)


def _edit() -> int:
    path = paths.policy_file()
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    with tempfile.TemporaryDirectory(prefix="hal-policy-") as scratch:
        copy = Path(scratch) / "policy.toml"
        copy.write_text(path.read_text(encoding="utf-8") if path.exists() else STARTER, encoding="utf-8")
        copy.chmod(0o600)
        subprocess.run([*editor, str(copy)], check=True, timeout=EDITOR_TIMEOUT_S)
        text = copy.read_text(encoding="utf-8")
        load_policy(copy)  # raises ConfigError: the live file is left alone
    _install(path, text)
    print(f"installed {path}")
    return 0


def _allow(opts: argparse.Namespace) -> int:
    if (opts.args is None) == (opts.domain is None):
        raise ValueError("give exactly one of --args JSON or --domain HOST")
    args = json.loads(opts.args) if opts.args is not None else None
    path = paths.policy_file()
    hub = Hub()
    session = hub.open_session(kind="hub", origin="cli")
    found = {t.name: t for t in hub.tools(session)}.get(opts.tool)
    meta = hub.tool_meta(found) if found else None
    new = edit.allow_rule(path, opts.tool, args=args, domain=opts.domain, meta=meta)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    diff = "".join(unified_diff(old.splitlines(True), new.splitlines(True), "policy.toml", "policy.toml (new)"))
    print(_safe(diff))
    if not sys.stdin.isatty():
        raise ValueError("hal policy allow needs a terminal to confirm; nothing was written")
    print("Type y and Enter to add this rule: ", end="", flush=True)
    if sys.stdin.readline().strip().lower() not in YES_ANSWERS:
        print("nothing written")
        return 1
    _install(path, new)
    print(f"updated {path}")
    return 0


def _audit(argv: list[str]) -> int:
    parser = _parser("audit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    tail = sub.add_parser("tail")
    tail.add_argument("-n", type=int, default=TAIL_DEFAULT)
    tail.add_argument("--session")
    tail.add_argument("--json", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--date")
    opts = parser.parse_args(argv)
    if opts.cmd == "verify":
        problems = audit.verify(date=opts.date)
        for problem in problems:
            print(escape(problem))
        print("audit log: ok" if not problems else f"audit log: {len(problems)} problem(s)")
        return 1 if problems else 0
    for record in audit.tail(n=opts.n, session=opts.session):
        print(json.dumps(record) if opts.json else escape(_line(record)))
    return 0


def _line(record: dict[str, object]) -> str:
    keys = ("ts", "session", "event", "tool", "verdict", "rule", "status")
    return "  ".join(f"{k}={record[k]}" for k in keys if k in record)

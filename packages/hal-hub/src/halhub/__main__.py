"""hal-hub - the command line for every Hal module.

    hal-hub tools [QUERY]             list tools, or the best matches for a request
    hal-hub modules                   installed modules
    hal-hub TOOL [key=value ...]      run a tool (or: hal-hub call TOOL --json '{...}')
    hal-hub TOOL --yes ...            run a tool that asks for confirmation
    hal-hub mcp                       serve the tools over MCP on stdio
    hal-hub doctor                    check every module's manifest and entry point
"""
from __future__ import annotations

import json
import sys

import halhub
from . import v1_registry as registry


def _print(data) -> None:
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(data)


def _args(argv: list[str]) -> dict:
    if "--json" in argv:
        return json.loads(argv[argv.index("--json") + 1])
    out = {}
    for a in argv:
        if a == "--yes":
            out["_confirmed"] = True
        elif "=" in a:
            k, v = a.split("=", 1)
            out[k] = v
        else:
            raise SystemExit(f"expected key=value, got {a!r}")
    return out


def doctor() -> int:
    bad = 0
    for m in registry.modules():
        status = "ok"
        if m.python:
            sys.path.insert(0, str(m.root))
            try:
                mod = __import__(m.python, fromlist=["_"])
                missing = [t.function for t in m.tools if not hasattr(mod, t.function)]
                if missing:
                    status, bad = f"missing functions: {', '.join(missing)}", bad + 1
            except Exception as e:  # noqa: BLE001
                status, bad = f"import failed: {e}", bad + 1
        print(f"  {m.name:14} {len(m.tools):3} tools  {status}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "tools":
        found = halhub.find(" ".join(rest), 15) if rest else sorted(halhub.all_tools().values(), key=lambda t: t.name)
        for t in found:
            print(f"  {t.name:28} {t.description.splitlines()[0][:90]}")
        return 0
    if cmd == "modules":
        from .tools import modules
        for m in modules():
            print(f"  {m['module']:14} {len(m['tools']):3} tools  {m['summary']}")
        return 0
    if cmd == "mcp":
        from . import mcp
        mcp.serve()
        return 0
    if cmd == "doctor":
        return doctor()
    if cmd == "call":
        cmd, rest = rest[0], rest[1:]
    out = halhub.call(cmd, _args(rest))
    _print(out.get("result", out) if "result" in out else out)
    return 1 if "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())

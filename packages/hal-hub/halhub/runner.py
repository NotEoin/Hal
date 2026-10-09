"""Calls a tool: in-process for stdlib Python modules, as a subprocess otherwise.

Arguments are checked against the tool's declared parameters and coerced from
strings when they come from a command line. A tool marked `confirm` returns a
request for confirmation unless called with `_confirmed: true`, so a voice or
chat client can ask the user first. Every call is appended to a JSON-lines log.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from .registry import Tool

LOG = Path(os.environ.get("HAL_STATE", "~/.local/state/hal")).expanduser() / "hub-calls.jsonl"


class ToolError(Exception):
    pass


def _coerce(value, kind: str):
    if not isinstance(value, str):
        return value
    if kind == "integer":
        return int(value)
    if kind == "number":
        return float(value)
    if kind == "boolean":
        return value.strip().lower() in ("1", "true", "yes", "on")
    if kind == "array":
        return [v.strip() for v in value.split(",") if v.strip()]
    if kind == "object":
        return json.loads(value)
    return value


def prepare(tool: Tool, args: dict) -> dict:
    """Returns the arguments checked and coerced; raises ToolError on a bad call."""
    known = {p.name: p for p in tool.params}
    clean = {}
    for k, v in args.items():
        if k.startswith("_"):
            continue
        if k not in known:
            raise ToolError(f"{tool.name} has no parameter '{k}'; it takes {', '.join(known) or 'none'}")
        try:
            clean[k] = _coerce(v, known[k].type)
        except (ValueError, json.JSONDecodeError) as e:
            raise ToolError(f"{tool.name}: '{k}' should be {known[k].type}: {e}") from e
    missing = [p.name for p in tool.params if p.required and p.name not in clean]
    if missing:
        raise ToolError(f"{tool.name} needs {', '.join(missing)}")
    return clean


def _in_process(tool: Tool, args: dict):
    root = str(tool.module.root)
    if root not in sys.path:
        sys.path.insert(0, root)
    mod = importlib.import_module(tool.module.python)
    fn = getattr(mod, tool.function)
    return fn(**args)


def _subprocess(tool: Tool, args: dict):
    proc = subprocess.run([*tool.module.exec, tool.function], input=json.dumps(args),
                          capture_output=True, text=True, timeout=tool.timeout, cwd=tool.module.root)
    if proc.returncode != 0:
        raise ToolError((proc.stderr or proc.stdout).strip()[-800:] or f"exit {proc.returncode}")
    out = proc.stdout.strip()
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return out


def call(tool: Tool, args: dict | None = None) -> dict:
    """Runs a tool and returns {"result": ...}, {"error": ...} or {"needs_confirmation": ...}."""
    args = dict(args or {})
    confirmed = bool(args.get("_confirmed"))
    started = time.time()
    try:
        clean = prepare(tool, args)
        if tool.confirm and not confirmed:
            return {"needs_confirmation": f"{tool.name} {json.dumps(clean)}",
                    "hint": "ask the user, then call again with _confirmed: true"}
        result = _in_process(tool, clean) if tool.module.python else _subprocess(tool, clean)
        out = {"result": result}
    except ToolError as e:
        out = {"error": str(e)}
    except subprocess.TimeoutExpired:
        out = {"error": f"{tool.name} took longer than {tool.timeout:.0f}s"}
    except Exception as e:  # noqa: BLE001 - a tool's failure is reported to the caller, never raised
        out = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=3)[-600:]}
    _log(tool, args, out, time.time() - started)
    return out


def _log(tool: Tool, args: dict, out: dict, seconds: float) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool.name, "args": args,
                                "ok": "error" not in out, "seconds": round(seconds, 3)}, default=str) + "\n")
    except OSError:
        pass

"""`hal-hub new NAME`: the skeleton of a v2 tool pack (04-hub.md, "`hal-hub new`").

`create("weather", directory)` writes `hal-weather/` with a `pyproject.toml`, README, MIT
LICENSE, the package `src/hal_weather/` (entry point, `hal-module.toml` with one `read` tool,
the tool function) and a behaviour-named test. It replaces `hal-ecosystem/scaffold.py`.

Design decisions:

- No copies of shared helpers (`config.py`, `notify.py`, `llm.py`): packs use `halkit`.
- The manifest sits inside the package, so it ships in the wheel and discovery finds it from
  the `hal.packs` entry point without importing the pack.
- `exec_worker=True` writes the shape of a pack that must not import Hal (hal-voice): an
  `exec` manifest and a stdlib worker script that speaks the hub's worker protocol, with no
  `hal-kit` or `hal-hub` dependency and no command of its own.
- Every target is checked before anything is written, so a refusal to overwrite leaves the
  directory as it was.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from halkit import proc
from halkit.timeutil import utc_now

log = logging.getLogger(__name__)

__all__ = ["NAME_RE", "create", "licence_holder"]

NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,31}$")  # 04-hub `[module] name`: also the tool prefix
GIT_TIMEOUT_S = 5.0  # `git config` reads one file; anything slower is a broken git
FALLBACK_HOLDER = "the authors"

PYPROJECT = """\
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "hal-{name}"
version = "0.1.0"
description = "{name} tools for Hal and any MCP client"
readme = "README.md"
requires-python = ">=3.11"
license = {{ text = "MIT" }}
dependencies = [{dependencies}]
{scripts}
[project.entry-points."hal.packs"]
{name} = "hal_{name}"

[tool.hatch.build.targets.wheel]
packages = ["src/hal_{name}"]

[dependency-groups]
dev = ["pytest>=8"]
"""

SCRIPTS = """
[project.scripts]
hal-{name} = "hal_{name}:main"
"""

README = """\
# hal-{name}

{name} tools for Hal. The pack's tools are listed in `src/hal_{name}/hal-module.toml` and
written in `src/hal_{name}/tools.py`.

## Install

```bash
uv pip install -e .            # or: pip install -e .
```

Hal's hub finds the pack through its `hal.packs` entry point.

## Run

{run}

## Test

```bash
uv run --group dev pytest      # or: PYTHONPATH=src python -m pytest
```
"""

RUN_PACK = """\
```bash
hal-{name} tools               # the pack's tools
hal-{name} call {name}_hello name=Ada
hal-{name} mcp                 # serve them to an MCP client on stdio
```"""

RUN_EXEC = """\
The hub runs `src/hal_{name}/worker.py` as the pack's worker. It imports nothing from Hal, so
the pack works with any Python 3.11 or later:

```bash
hal-hub call {name}_hello name=Ada
```"""

LICENSE = """\
MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

INIT_PACK = '''\
"""The {name} pack: `hal-{name} mcp|tools|call|doctor`, with its tools in `tools.py`."""

from halhub import pack

PACK = "{name}"
MODULES = ("{name}",)


def main() -> int:
    """Runs the pack's command line and returns its exit code."""
    return pack.main(PACK, MODULES)
'''

INIT_EXEC = '''\
"""The {name} pack: tools in `tools.py`, run by Hal's hub through `worker.py` (no Hal imports)."""
'''

MANIFEST = """\
# The {name} pack's tools for Hal's hub (manifest schema 2).
schema = 2

[module]
name = "{name}"
pack = "{name}"
summary = "{name} tools"
{runner}
keywords = ["{name}"]

[[tools]]
name = "{name}_hello"
function = "hello"
description = "Greet someone by name. An example tool: replace it with the pack's own."
tier = "read"
[tools.params.name]
description = "who to greet"
required = true
max_length = 100
"""

TOOLS = '''\
"""The {name} pack's tool functions; each one is a `[[tools]]` entry in `hal-module.toml`."""

from __future__ import annotations


def hello(name: str) -> str:
    """A greeting for `name`."""
    return f"Hello, {{name}}."
'''

TEST = '''\
"""Tests for the {name} pack's tools."""

from __future__ import annotations

from hal_{name}.tools import hello


def test_hello_greets_by_name():
    assert hello("Ada") == "Hello, Ada."
'''

WORKER = '''\
"""The {name} pack's worker: Hal's worker protocol on stdin and stdout, stdlib only.

The hub starts this script, reads one `ready` line, then sends one JSON `call` per line and
reads one `result` per call. Protocol: Hal's docs/dev/worker-protocol.md (version 1).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import platform
import sys
import traceback
from pathlib import Path

PROTOCOL = 1
ERROR_CHARS = 2000  # the protocol cuts exception text to this length


def _load_tools():
    """`tools.py` beside this script, loaded by path so the worker needs nothing installed."""
    path = Path(__file__).with_name("tools.py")
    spec = importlib.util.spec_from_file_location("hal_{name}_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signature(fn) -> dict:
    params = [
        {{"name": p.name, "required": p.default is inspect.Parameter.empty}}
        for p in inspect.signature(fn).parameters.values()
    ]
    return {{"params": params, "var_keyword": False, "async": inspect.iscoroutinefunction(fn)}}


def main() -> int:
    """Serves calls until `shutdown` or the end of stdin."""
    wire = sys.stdout
    sys.stdout = sys.stderr  # a print in a tool must never reach the protocol stream

    def send(message: dict) -> None:
        wire.write(json.dumps({{"v": PROTOCOL, **message}}, default=str, allow_nan=False) + "\\n")
        wire.flush()

    tools = _load_tools()
    functions = {{
        name: fn
        for name, fn in vars(tools).items()
        if inspect.isfunction(fn) and fn.__module__ == tools.__name__ and not name.startswith("_")
    }}
    send({{
        "op": "ready",
        "pid": os.getpid(),
        "python": platform.python_version(),
        "module": "hal_{name}.tools",
        "functions": {{name: _signature(fn) for name, fn in functions.items()}},
        "missing": [],
    }})
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            send({{"op": "result", "id": None, "ok": False,
                  "error": {{"type": "ProtocolError", "message": "not JSON"}}}})
            continue
        op, request_id = message.get("op"), message.get("id")
        if op == "shutdown":
            return 0
        if op == "ping":
            send({{"op": "pong", "id": request_id}})
            continue
        if op != "call":
            send({{"op": "result", "id": request_id, "ok": False,
                  "error": {{"type": "ProtocolError", "message": f"unknown op {{op!r}}"}}}})
            continue
        fn = functions.get(message.get("function"))
        args = message.get("args") or {{}}
        if fn is None:
            send({{"op": "result", "id": request_id, "ok": False,
                  "error": {{"type": "ProtocolError", "message": "no such function"}}}})
            continue
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as exc:
            send({{"op": "result", "id": request_id, "ok": False,
                  "error": {{"type": "ArgumentsMismatch", "message": str(exc)}}}})
            continue
        try:
            result = fn(**args)
            send({{"op": "result", "id": request_id, "ok": True, "result": result}})
        except Exception as exc:  # a tool's failure goes to the caller; the traceback to stderr
            traceback.print_exc()
            send({{"op": "result", "id": request_id, "ok": False,
                  "error": {{"type": type(exc).__name__, "message": str(exc)[:ERROR_CHARS]}}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def create(name: str, directory: Path, *, exec_worker: bool = False, holder: str | None = None) -> list[Path]:
    """Writes `hal-<name>/` under `directory` and returns the files written.

    Raises `ValueError` for a name that does not match `NAME_RE`, and `FileExistsError` when
    any file it would write already exists (nothing is written then).
    """
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"pack name {name!r} must match {NAME_RE.pattern} (lowercase letters and digits)")
    root = Path(directory) / f"hal-{name}"
    package = root / "src" / f"hal_{name}"
    fields = {"name": name}
    if exec_worker:
        runner = 'exec = ["{python}", "{root}/worker.py"]\nprotocol = "jsonl"'
        pyproject = PYPROJECT.format(dependencies="", scripts="", **fields)
        init, run = INIT_EXEC, RUN_EXEC
    else:
        runner = f'python = "hal_{name}.tools"'
        dependencies = '"hal-kit>=0.1,<0.2", "hal-hub>=0.1,<0.2"'
        pyproject = PYPROJECT.format(dependencies=dependencies, scripts=SCRIPTS.format(**fields), **fields)
        init, run = INIT_PACK, RUN_PACK
    files = {
        root / "pyproject.toml": pyproject,
        root / "README.md": README.format(run=run.format(**fields), **fields),
        root / "LICENSE": LICENSE.format(year=utc_now().year, holder=holder or licence_holder()),
        package / "__init__.py": init.format(**fields),
        package / "hal-module.toml": MANIFEST.format(runner=runner, **fields),
        package / "tools.py": TOOLS.format(**fields),
        root / "tests" / "test_tools.py": TEST.format(**fields),
    }
    if exec_worker:
        files[package / "worker.py"] = WORKER.format(**fields)
    existing = [path for path in files if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite {', '.join(str(p) for p in existing)}")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    log.info("created %s", root)
    return list(files)


def licence_holder() -> str:
    """`git config user.name`, or `FALLBACK_HOLDER` when git has none or cannot run."""
    try:
        done = proc.run(["git", "config", "user.name"], timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, proc.ProcTimeout) as exc:
        log.info("no git user name for the licence: %s", exc)
        return FALLBACK_HOLDER
    name = done.stdout.decode(errors="replace").strip()
    return name if done.returncode == 0 and name.isascii() and name else FALLBACK_HOLDER

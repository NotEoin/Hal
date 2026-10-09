"""Finds Hal modules and the tools they declare in their hal-module.toml manifests.

A module is any directory holding a hal-module.toml. Modules are looked for in
the directories named by HAL_MODULE_PATH (colon-separated), else by
`module_paths` in ~/.config/hal/config.toml, else in the hal-ecosystem folder
beside this repository. Stdlib only.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("HAL_CONFIG", "~/.config/hal/config.toml")).expanduser()
MANIFEST = "hal-module.toml"


@dataclass
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list | None = None
    default: object = None


@dataclass
class Tool:
    name: str
    description: str
    module: "Module"
    function: str
    params: list[Param] = field(default_factory=list)
    confirm: bool = False           # needs the user's say-so before running
    timeout: float = 60.0           # seconds
    keywords: str = ""

    def schema(self) -> dict:
        """Returns the tool as a JSON-schema function definition (Ollama and MCP use this shape)."""
        props = {}
        for p in self.params:
            prop = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            if p.type == "array":
                prop["items"] = {"type": "string"}
            props[p.name] = prop
        return {"type": "object", "properties": props,
                "required": [p.name for p in self.params if p.required]}

    def summary(self) -> dict:
        return {"name": self.name, "module": self.module.name, "description": self.description,
                "parameters": self.schema(), "confirm": self.confirm}


@dataclass
class Module:
    name: str
    root: Path
    summary: str = ""
    python: str | None = None        # import path, called in-process
    exec: list[str] | None = None    # command, called as a subprocess with JSON on stdin
    tools: list[Tool] = field(default_factory=list)


def load_config() -> dict:
    """Returns the shared ecosystem config, or {} if there is none."""
    if CONFIG_PATH.exists():
        return tomllib.loads(CONFIG_PATH.read_text())
    return {}


def search_paths() -> list[Path]:
    env = os.environ.get("HAL_MODULE_PATH")
    if env:
        return [Path(p).expanduser() for p in env.split(":") if p]
    configured = load_config().get("hub", {}).get("module_paths")
    if configured:
        return [Path(p).expanduser() for p in configured]
    return [Path(__file__).resolve().parents[2]]


def load_module(root: Path) -> Module:
    """Reads one module's manifest."""
    data = tomllib.loads((root / MANIFEST).read_text())
    info = data.get("module", {})
    module = Module(name=info["name"], root=root, summary=info.get("summary", ""),
                    python=info.get("python"),
                    exec=[a.replace("{root}", str(root)) for a in info["exec"]] if info.get("exec") else None)
    for t in data.get("tools", []):
        params = [Param(name=n, type=p.get("type", "string"), description=p.get("description", ""),
                        required=p.get("required", False), enum=p.get("enum"), default=p.get("default"))
                  for n, p in t.get("params", {}).items()]
        module.tools.append(Tool(name=t["name"], description=t["description"].strip(), module=module,
                                 function=t.get("function", t["name"]), params=params,
                                 confirm=t.get("confirm", False), timeout=float(t.get("timeout", 60)),
                                 keywords=t.get("keywords", "")))
    return module


def modules() -> list[Module]:
    """Returns every installed module, skipping any listed in config [hub].disabled."""
    disabled = set(load_config().get("hub", {}).get("disabled", []))
    found: dict[str, Module] = {}
    for base in search_paths():
        if not base.is_dir():
            continue
        candidates = [base] if (base / MANIFEST).exists() else sorted(base.iterdir())
        for d in candidates:
            if (d / MANIFEST).exists():
                try:
                    m = load_module(d)
                except (OSError, KeyError, tomllib.TOMLDecodeError) as e:
                    print(f"hal-hub: skipping {d}: {e}", flush=True)
                    continue
                if m.name not in disabled:
                    found.setdefault(m.name, m)
    return list(found.values())


def tools() -> dict[str, Tool]:
    """Returns every tool of every installed module, by name."""
    return {t.name: t for m in modules() for t in m.tools}

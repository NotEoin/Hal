"""Mode presets: discovery, the `[hal]` section, rendering for llama-server (REQ-GW-75, 78).

A mode is an INI file of llama-server presets plus one `[hal]` section that wires the
gateway's policies. Decisions:

- Rendering is a token substitution on the text, then a `configparser` parse for the checks,
  then the text minus `[hal]` is written. Comments survive.
- A mode that sets a forbidden key, names a weight file the manifest does not pin, or uses
  an unpinned chat template is refused before any file is written, so llama-server never
  sees it (REQ-SEC-76, 79, 81). Only the first shard of a split GGUF is named in a preset,
  so only that shard is hashed; llama-server finds the others by name [unverified: later
  shards are not checked].
- A user file in `[router].modes_dirs` replaces the bundled mode of the same name.
"""

from __future__ import annotations

import configparser
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .models.manifest import PICKLE_SUFFIXES, ManifestError, Verifier, sha256_file

log = logging.getLogger(__name__)

BUNDLED = Path(__file__).resolve().parent / "modes"
SUMS_NAME = "SHA256SUMS"
EFFORT_NOTE = BUNDLED / "effort-xhigh.txt"
HAL_SECTION = "hal"
TOP = "__top__"  # llama.cpp presets allow `version = 1` before the first section
RENDERED_MODE = 0o600
MODEL_KEYS = ("model", "model-draft", "mmproj", "m", "md", "mm")
TEMPLATE_KEY = "chat-template-file"
DEFAULT_MODELS_MAX = 3  # hal-serve's default
DEFAULT_SWAP_SLOTS = 2
DEFAULT_STRIP_TOOLS = "Bash,Edit,MultiEdit,Write,NotebookEdit"
DENIED_KEYS = frozenset(
    "host port api-key api-key-file ssl-key-file ssl-cert-file api-prefix path cors-origins "
    "cors-methods cors-headers cors-credentials tools tools-runtime agent mcp-servers-config "
    "mcp-servers-json ui-mcp-proxy webui-mcp-proxy ui webui props media-path log-prompts-dir rpc "
    "models-dir models-preset models-max models-autoload hf-repo hf-file hf-repo-draft hf-token "
    "model-url docker-repo hf hfr hff hfd hfrd hft mu".split()
)
_HAL_KEYS = {
    "description",
    "main",
    "agent",
    "classifier",
    "models-max",
    "autoload",
    "route",
    "effort",
    "effort-note",
    "delegate-note",
    "strip-tools",
    "swap",
    "swap-slots",
    "context-window",
    "client-timeout-ms",
}
_SECTION = re.compile(r"^\[(.+)\]\s*$")


class ModeError(ValueError):
    """A mode is unknown, malformed, or refused by a security check."""


@dataclass(frozen=True)
class HalSettings:
    """The parsed `[hal]` section of a mode."""

    description: str
    main: str
    agent: str
    classifier: str | None
    models_max: int
    autoload: bool
    route: bool
    effort: bool
    effort_note: Path
    delegate_note: Path | None
    strip_tools: tuple[str, ...]
    swap: bool
    swap_slots: int
    context_window: int
    client_timeout_ms: int | None
    preset_windows: dict[str, int] = field(default_factory=dict)  # preset name -> its `ctx-size`


@dataclass(frozen=True)
class RenderedMode:
    """A mode written for llama-server."""

    name: str
    path: Path
    sha256: str
    hal: HalSettings


def mode_dirs(user_dirs: list[str] | tuple[str, ...]) -> list[Path]:
    """Search order: the user's directories, then the bundled one."""
    return [Path(d).expanduser() for d in user_dirs] + [BUNDLED]


def discover(user_dirs: list[str] | tuple[str, ...]) -> dict[str, Path]:
    """Mode name to file; the first directory that has the name wins."""
    found: dict[str, Path] = {}
    for directory in mode_dirs(user_dirs):
        if directory.is_dir():
            for path in sorted(directory.glob("*.ini")):
                found.setdefault(path.stem, path)
    return found


def _parse(text: str, name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True, default_section="")
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
    try:
        parser.read_string(f"[{TOP}]\n{text}", source=name)
    except configparser.Error as exc:
        raise ModeError(f"mode {name}: {exc}") from exc
    return parser


def _strip_hal(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        match = _SECTION.match(line.strip())
        if match:
            skipping = match.group(1) == HAL_SECTION
        if not skipping:
            out.append(line)
    return "".join(out)


def _flag(raw: dict[str, str], key: str, default: bool, name: str) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if value.lower() not in ("true", "false"):
        raise ModeError(f"mode {name}: [hal] {key} must be true or false")
    return value.lower() == "true"


def _int(raw: dict[str, str], key: str, default: int | None, name: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return default
    if not value.isdigit() or int(value) <= 0:
        raise ModeError(f"mode {name}: [hal] {key} must be a positive integer")
    return int(value)


def _preset_windows(parser: configparser.ConfigParser, name: str) -> dict[str, int]:
    """Each preset's `ctx-size` (with `[*]` defaults); a preset without one is left out."""
    shared = dict(parser["*"]) if parser.has_section("*") else {}
    windows = {}
    for section in parser.sections():
        if section in (TOP, HAL_SECTION, "*"):
            continue
        value = {**shared, **dict(parser[section])}.get("ctx-size")
        if value is None:
            continue
        if not value.isdigit():
            raise ModeError(f"mode {name}: preset {section} ctx-size {value!r} is not a number")
        windows[section] = int(value)
    return windows


def _context_window(parser: configparser.ConfigParser, raw: dict[str, str], name: str) -> int:
    """Smallest `ctx-size`, skipping the classifier and, in swap modes, the agent (hal:159-168)."""
    skip = {raw.get("classifier")}
    if _flag(raw, "swap", False, name):
        skip.add(raw.get("agent"))
    windows = _preset_windows(parser, name)
    sizes = []
    for section in parser.sections():
        if section in (TOP, HAL_SECTION, "*") or section in skip:
            continue
        if section not in windows:
            raise ModeError(f"mode {name}: preset {section} has no ctx-size and [*] has none")
        sizes.append(windows[section])
    if not sizes:
        raise ModeError(f"mode {name}: no preset sets ctx-size")
    return min(sizes)


def _hal_settings(parser: configparser.ConfigParser, name: str) -> HalSettings:
    if not parser.has_section(HAL_SECTION):
        raise ModeError(f"mode {name}: no [hal] section")
    raw = dict(parser[HAL_SECTION])
    unknown = set(raw) - _HAL_KEYS
    if unknown:
        raise ModeError(f"mode {name}: unknown [hal] keys {sorted(unknown)}")
    main = raw.get("main")
    if not main:
        raise ModeError(f"mode {name}: [hal] main is required")
    agent, classifier = raw.get("agent", main), raw.get("classifier") or None
    for key, preset in (("main", main), ("agent", agent), ("classifier", classifier)):
        if preset is not None and not parser.has_section(preset):
            raise ModeError(f"mode {name}: [hal] {key} names {preset!r}, which is not a preset here")
    route = _flag(raw, "route", False, name)
    if route and (classifier is None or "agent" not in raw):
        raise ModeError(f"mode {name}: route = true needs agent and classifier")
    note = raw.get("delegate-note")
    window = _int(raw, "context-window", None, name) or _context_window(parser, raw, name)
    return HalSettings(
        description=raw.get("description", ""),
        main=main,
        agent=agent,
        classifier=classifier,
        models_max=_int(raw, "models-max", DEFAULT_MODELS_MAX, name),  # type: ignore[arg-type]
        autoload=_flag(raw, "autoload", True, name),
        route=route,
        effort=_flag(raw, "effort", False, name),
        effort_note=Path(raw["effort-note"]) if "effort-note" in raw else EFFORT_NOTE,
        delegate_note=Path(note) if note else None,
        strip_tools=tuple(t for t in raw.get("strip-tools", DEFAULT_STRIP_TOOLS).split(",") if t),
        swap=_flag(raw, "swap", False, name),
        swap_slots=_int(raw, "swap-slots", DEFAULT_SWAP_SLOTS, name),  # type: ignore[arg-type]
        context_window=window,
        client_timeout_ms=_int(raw, "client-timeout-ms", None, name),
        preset_windows=_preset_windows(parser, name),
    )


def _check_keys(parser: configparser.ConfigParser, name: str) -> None:
    for section in parser.sections():
        if section == HAL_SECTION:
            continue
        for key in parser[section]:
            bare = key.lstrip("-").lower()
            if bare in DENIED_KEYS or bare.endswith("-default"):
                raise ModeError(f"mode {name}: preset key {key!r} in [{section}] is not allowed")


def _bundled_sums() -> dict[str, str]:
    sums = {}
    for line in (BUNDLED / SUMS_NAME).read_text(encoding="ascii").splitlines():
        digest, _, file = line.partition("  ")
        if digest and file:
            sums[file] = digest
    return sums


def _template_ok(template: Path, pinned: set[str]) -> bool:
    digest = sha256_file(template)
    in_bundle = template.resolve().parent == BUNDLED and _bundled_sums().get(template.name) == digest
    return in_bundle or digest in pinned


def _check_files(parser: configparser.ConfigParser, name: str, verifier: Verifier) -> None:
    shared = dict(parser["*"]) if parser.has_section("*") else {}
    for section in parser.sections():
        if section in (TOP, HAL_SECTION):
            continue
        keys = {**shared, **dict(parser[section])}
        pinned: set[str] = set()
        for key in MODEL_KEYS:
            if key not in keys:
                continue
            path = Path(keys[key])
            if path.suffix.lower() in PICKLE_SUFFIXES:
                raise ModeError(f"mode {name}: [{section}] {key} is a pickle format and is refused")
            try:
                entry = verifier.verify_path(path)
            except ManifestError as exc:
                raise ModeError(f"mode {name}: [{section}] {key}: {exc}") from exc
            if entry.template_sha256:
                pinned.add(entry.template_sha256)
        if TEMPLATE_KEY in keys:
            template = Path(keys[TEMPLATE_KEY])
            if not template.is_file() or not _template_ok(template, pinned):
                raise ModeError(
                    f"mode {name}: [{section}] chat-template-file {template} is not a bundled "
                    "template and no model entry pins its sha256 (template_sha256)"
                )


def list_modes(user_dirs: list[str] | tuple[str, ...]) -> list[tuple[str, str, Path]]:
    """`(name, description, file)` for every discoverable mode; a broken file shows its error."""
    rows = []
    for mode, path in sorted(discover(user_dirs).items()):
        try:
            parser = _parse(path.read_text(encoding="utf-8"), mode)
            description = parser.get(HAL_SECTION, "description", fallback="")
        except (ModeError, OSError) as exc:
            description = f"invalid: {exc}"
        rows.append((mode, description, path))
    return rows


def render_mode(
    name: str,
    *,
    models_dir: Path,
    state_dir: Path,
    verifier: Verifier,
    user_dirs: list[str] | tuple[str, ...] = (),
) -> RenderedMode:
    """Renders mode `name` into `<state_dir>/rendered/<name>.ini`; raises `ModeError` if refused."""
    path = discover(user_dirs).get(name)
    if path is None:
        raise ModeError(f"unknown mode {name!r}; `hal-gateway modes` lists them")
    tokens = {
        "@MODELS@": str(models_dir),
        "@HAL@": str(BUNDLED.parent),
        "@MODES@": str(path.parent),
        "@STATE@": str(state_dir),
    }
    text = path.read_text(encoding="utf-8")
    for token, value in tokens.items():
        text = text.replace(token, value)
    parser = _parse(text, name)
    _check_keys(parser, name)
    hal = _hal_settings(parser, name)
    _check_files(parser, name, verifier)
    for note in (hal.effort_note, hal.delegate_note):
        if note is not None and not note.is_file():
            raise ModeError(f"mode {name}: note file {note} does not exist")
    body = _strip_hal(text)
    target = state_dir / "rendered" / f"{name}.ini"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, RENDERED_MODE)
    os.replace(tmp, target)
    return RenderedMode(name, target, hashlib.sha256(body.encode("utf-8")).hexdigest(), hal)

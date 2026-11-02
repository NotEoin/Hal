"""The model-configuration engine behind `hal tune` and `hal models`.

It joins the pieces in `halkit` (hardware, discovery, tuning, config) and the gateway (pinning,
the generated mode) into four steps:

1. `survey` lists the models of every source;
2. `build` picks a model per role (an assigned one stays; an empty role gets the best fit among
   local models) and plans its settings on this hardware;
3. `wire` works out what the gateway needs to serve the plan: `[hal]` model names, backends;
4. `apply` pins the models, writes the config and the generated mode.

Design decisions:

- Automatic choice never picks a cloud source or a remote server: a role on a cloud model is
  the user's explicit statement, made with `[models.roles.X]` (the local model is the private
  default, the cloud one an opt-in).
- Assigning a model pins it (the Ollama digest, or the sha256 of a GGUF file): the user's
  choice is their statement of trust, and the gateway refuses unpinned models.
- Nothing here downloads a model.
- An Anthropic source can be listed and assigned but the gateway has no Anthropic backend, so
  `wire` leaves such a role out of the gateway and says so in the plan notes.
"""

from __future__ import annotations

import logging
import re
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from halgateway import models as gwmodels
from halgateway import tunedmode
from halgateway.config import GATEWAY_TABLE, ROLE_ALIASES
from halgateway.models.manifest import ManifestError
from halkit import config as kitconfig
from halkit import discovery, files, modelconf, paths, tomledit, tuning
from halkit.hardware import Hardware
from halkit.modelinfo import ModelInfo

from . import confmigrate

log = logging.getLogger(__name__)

HAL_KEYS = {role: ROLE_ALIASES[f"hal-{role}"] for role in modelconf.ROLES}
LOCAL_KINDS = ("ollama", "openai", "gguf")
MIN_GOOD_CONTEXT = 16384  # a chat model that fits with less than this ranks below one that does
CPU_COMFORT_BYTES = 6 * 1024**3  # about an 8B model at Q4: tolerable speed without a GPU
CODE_NAME = re.compile(r"cod(e|er)\b|codestral|devstral", re.IGNORECASE)
EMBED_NAME = re.compile(r"embed|bge-|e5-|minilm|nomic", re.IGNORECASE)
ROUTER_BACKEND = "router"
DEFAULT_OLLAMA = "ollama"
OPENAI_URL = "https://api.openai.com/v1"


@dataclass
class RolePlan:
    """What the engine decided for one role."""

    role: str
    source: modelconf.Source | None = None
    model: ModelInfo | None = None
    plan: tuning.Plan | None = None
    assigned: bool = False  # True when the config already named this model
    locked: bool = False  # `auto = false`: the user's context, kv_type and gpu_layers stay
    manual: bool = False  # the plan holds numbers the user chose: write them and set `auto = false`
    settings: dict[str, object] = field(default_factory=dict)  # `effort` and `temperature` to write
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready view."""
        return {
            "role": self.role,
            "source": self.source.name if self.source else None,
            "model": self.model.to_dict() if self.model else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "assigned": self.assigned,
            "locked": self.locked,
            "notes": self.notes,
        }


Discover = Callable[[modelconf.Source], discovery.Discovery]


def survey(sources: Mapping[str, modelconf.Source], discover: Discover = discovery.discover) -> dict[str, discovery.Discovery]:
    """The discovery result of every source, by source name."""
    return {name: discover(src) for name, src in sources.items()}


def is_local(source: modelconf.Source) -> bool:
    """True for a source automatic choice may use: a local kind, not marked remote."""
    return source.kind in LOCAL_KINDS and not source.allow_remote


def _usable(model: ModelInfo, role: str) -> bool:
    is_embed = "embedding" in model.capabilities or bool(EMBED_NAME.search(model.name))
    if role == "embed":
        return is_embed
    if role == "vision":
        return "vision" in model.capabilities and not is_embed
    return not is_embed


def _rank(model: ModelInfo, plan: tuning.Plan | None) -> tuple[int, int, float]:
    """Higher is better: how well it fits, whether it takes tools, how big it is."""
    tools = int("tools" in model.capabilities)
    size = model.params_b or (model.size_bytes or 0) / 1e9
    if plan is None:
        return (0, tools, size)  # a server-managed model: usable, but we cannot size it
    if plan.status == "gpu":
        return (4 if plan.context >= MIN_GOOD_CONTEXT else 3, tools, size)
    if plan.status == "hybrid":
        return (2, tools, size)
    if plan.status == "cpu":
        return (2 if (model.size_bytes or 0) <= CPU_COMFORT_BYTES else 1, tools, size)
    return (-1, tools, size)


def _plan(model: ModelInfo, hw: Hardware, role: modelconf.Role | None, limits: Mapping[str, object]) -> tuning.Plan | None:
    if not model.size_bytes or model.kind in ("anthropic", "openai-cloud"):
        return None
    kv = (role.kv_type if role and role.kv_type else "") or str(limits["kv_type"])
    return tuning.plan_model(
        model,
        hw,
        kv_type=kv,
        context_cap=int(limits["context_cap"]),  # type: ignore[call-overload]
        headroom_override_mib=int(limits["headroom_mib"]),  # type: ignore[call-overload]
    )


def _find(found: Mapping[str, discovery.Discovery], source: str, name: str) -> ModelInfo | None:
    for model in found[source].models if source in found else ():
        if model.name == name or model.name == f"{name}:latest":
            return model
    return None


def choose(
    role: str, found: Mapping[str, discovery.Discovery], sources: Mapping[str, modelconf.Source], hw: Hardware,
    limits: Mapping[str, object], taken: Mapping[str, ModelInfo],
) -> RolePlan:
    """The best local model for an empty `role`, planned; an empty plan with a note when none fits."""
    best: tuple[tuple[int, int, float], ModelInfo, tuning.Plan | None] | None = None
    for name, src in sources.items():
        if not is_local(src) or name not in found:
            continue
        for model in found[name].models:
            if not _usable(model, role):
                continue
            if role == "code" and not CODE_NAME.search(model.name):
                continue
            try:
                plan = _plan(model, hw, None, limits)
            except ValueError:
                # a model that cannot be planned for this hardware is not a candidate
                continue
            rank = _rank(model, plan)
            if role in ("fast", "embed"):  # the smallest that works
                rank = (rank[0] >= 0, rank[1], -(model.size_bytes or 0))  # type: ignore[assignment]
            if rank[0] < 0 or (plan and plan.status == "does-not-fit"):
                continue
            if best is None or rank > best[0]:
                best = (rank, model, plan)
    if best is None:
        return RolePlan(role, notes=[f"no local model found for {role}"])
    _, model, plan = best
    out = RolePlan(role, sources[model.source], model, plan, notes=["chosen automatically"])
    if role == "fast" and "chat" in taken and taken["chat"].name == model.name:
        out.notes.append("same model as chat")
    return out


def build(
    roles: Mapping[str, modelconf.Role], sources: Mapping[str, modelconf.Source],
    found: Mapping[str, discovery.Discovery], hw: Hardware, limits: Mapping[str, object], *,
    only: tuple[str, ...] = (), reassign: bool = False,
) -> list[RolePlan]:
    """A `RolePlan` per role (or per role in `only`): assigned models are kept unless `reassign`."""
    plans: list[RolePlan] = []
    taken: dict[str, ModelInfo] = {}
    for role in modelconf.ROLES:
        if only and role not in only:
            continue
        cfg = roles.get(role)
        if cfg is not None and not reassign:
            model = _find(found, cfg.source, cfg.model)
            rp = RolePlan(role, sources[cfg.source], model, assigned=True, locked=not cfg.auto)
            if model is None:
                why = found[cfg.source].error if cfg.source in found and found[cfg.source].error else "not listed"
                rp.notes.append(f"{cfg.model} not found in {cfg.source} ({why})")
            else:
                try:
                    rp.plan = _plan(model, hw, cfg, limits)
                except ValueError as exc:
                    rp.notes.append(str(exc))
            plans.append(rp)
        else:
            rp = choose(role, found, sources, hw, limits, taken)
            if rp.model and role in ("chat", "fast"):
                taken[role] = rp.model
            if role == "writer" and cfg is None:
                continue  # writer falls back to chat unless the user names one
            plans.append(rp)
        if plans[-1].plan is None and plans[-1].model is not None:
            plans[-1].notes.append("size unknown: the server decides context and offload")
    return plans


# --- wiring ----------------------------------------------------------------------------------


@dataclass
class Wiring:
    """What the gateway needs: `[hal]` model names, new backends and the tuned mode's presets."""

    hal: dict[str, object] = field(default_factory=dict)
    backends: dict[str, dict[str, object]] = field(default_factory=dict)
    presets: list[tunedmode.Preset] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _existing_backends(doc: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    gateway = doc.get("gateway")
    backends = gateway.get("backends") if isinstance(gateway, dict) else None
    return backends if isinstance(backends, dict) else {}


def wire(plans: list[RolePlan], doc: Mapping[str, object]) -> Wiring:  # noqa: C901 - one branch per source kind
    """The gateway settings that serve `plans`; `doc` is the parsed current config."""
    out = Wiring()
    existing = _existing_backends(doc)
    hal_now = doc.get("hal") if isinstance(doc.get("hal"), dict) else {}
    default_ollama: str | None = None
    for rp in plans:
        if rp.model is None or rp.source is None:
            continue
        src, model = rp.source, rp.model
        key = HAL_KEYS[rp.role]
        if src.kind == "ollama":
            default_ollama = default_ollama or src.name
            if src.name == default_ollama:
                out.hal[key] = model.name
                out.hal["ollama_url"] = src.url
                continue
            out.backends.setdefault(src.name, {"type": "ollama", "url": src.url})
            out.hal[key] = f"{src.name}/{model.name}"
        elif src.kind == "openai":
            entry: dict[str, object] = {"type": "openai", "url": src.url}
            if src.api_key_file:
                entry["api_key_file"] = src.api_key_file
            out.backends.setdefault(src.name, entry)
            out.hal[key] = f"{src.name}/{model.name}"
        elif src.kind == "openai-cloud":
            out.backends.setdefault(
                src.name, {"type": "openai", "url": OPENAI_URL, "allow_remote": True, "api_key_file": src.api_key_file}
            )
            out.hal[key] = f"{src.name}/{model.name}"
        elif src.kind == "anthropic":
            out.notes.append(f"{rp.role}: the gateway has no Anthropic backend; the role is not wired")
        elif src.kind == "gguf":
            if rp.plan is None or rp.plan.status == "does-not-fit":
                out.notes.append(f"{rp.role}: {model.name} does not fit; not wired")
                continue
            router = next((n for n, b in existing.items() if b.get("type") == "llama-router"), None)
            if router is None:
                router = ROUTER_BACKEND
                out.backends.setdefault(
                    router,
                    {"type": "llama-router", "managed": True, "models_dir": str(Path(src.path).expanduser()), "mode": tunedmode.MODE_NAME},
                )
            elif existing[router].get("mode", "gpu") != tunedmode.MODE_NAME:
                out.notes.append(f"set mode = \"tuned\" in [gateway.backends.{router}] to serve {rp.role} from the GGUF file")
            out.hal[key] = f"{router}/{tunedmode.preset_name(rp.role)}"
            out.presets.append(_preset(rp))
    if out.backends:
        for name in list(out.backends):
            if name in existing:
                out.backends.pop(name)
                out.notes.append(f"kept your [gateway.backends.{name}]")
        if not existing and DEFAULT_OLLAMA not in out.backends:
            out.backends[DEFAULT_OLLAMA] = {"type": "ollama"}  # the implicit default ends when any backend exists
    if hal_now.get("ollama_url") == out.hal.get("ollama_url"):  # type: ignore[union-attr]
        out.hal.pop("ollama_url", None)
    return out


def _preset(rp: RolePlan) -> tunedmode.Preset:
    assert rp.model is not None and rp.plan is not None
    p = rp.plan
    return tunedmode.Preset(
        role=rp.role, model_path=Path(rp.model.path), context=p.context, kv_type=p.kv_type, gpu_layers=p.gpu_layers,
        tensor_split=p.tensor_split, embedding=rp.role == "embed", mmproj=_mmproj(Path(rp.model.path)) if rp.role == "vision" else None,
    )


def _mmproj(model: Path) -> Path | None:
    found = sorted(model.parent.glob("mmproj*.gguf"))
    return found[0] if found else None


# --- applying --------------------------------------------------------------------------------


def role_table(rp: RolePlan, current: Mapping[str, object]) -> dict[str, object]:
    """The `[models.roles.X]` values to write: tuned numbers unless the role is locked."""
    assert rp.source is not None and rp.model is not None
    values: dict[str, object] = {"source": rp.source.name, "model": rp.model.name}
    for key in ("effort", "temperature", "family"):
        if current.get(key) not in (None, "", -1.0):
            values[key] = current[key]
    if rp.model.family and "family" not in values and rp.assigned is False:
        values["family"] = rp.model.family
    for key, value in rp.settings.items():
        if value in (None, "", modelconf.UNSET_TEMPERATURE):
            values.pop(key, None)
        else:
            values[key] = value
    locked = (rp.locked and not rp.manual) or rp.plan is None
    if rp.plan is not None and not locked:
        values.update(context=rp.plan.context, kv_type=rp.plan.kv_type, gpu_layers=rp.plan.gpu_layers)
    else:
        for key in ("context", "kv_type", "gpu_layers"):
            if key in current:
                values[key] = current[key]
    if locked or rp.manual:
        values["auto"] = False
    return values


def edit_config(text: str, plans: list[RolePlan], wiring: Wiring) -> str:
    """The new config text: role tables, `[hal]` model names and any new backends."""
    doc = tomllib.loads(text) if text.strip() else {}
    current_roles = (doc.get("models") or {}).get("roles", {}) if isinstance(doc.get("models"), dict) else {}  # type: ignore[union-attr]
    for rp in plans:
        if rp.model is None or rp.source is None:
            continue
        text = tomledit.set_table(text, f"models.roles.{rp.role}", role_table(rp, current_roles.get(rp.role, {})))
    if wiring.hal:
        text = tomledit.set_keys(text, "hal", wiring.hal)
    for name, entry in wiring.backends.items():
        text = tomledit.set_table(text, f"gateway.backends.{name}", entry)
    return text


class _Tags:
    """Gives `record_ollama` the digests discovery already read, so no second request is made."""

    def __init__(self, found: Mapping[str, discovery.Discovery]) -> None:
        self._found = found

    def tags(self) -> list[dict[str, str]]:
        return [{"name": m.name, "digest": m.digest} for d in self._found.values() for m in d.models if m.digest]


def manifest_store() -> gwmodels.Store:
    """The model manifest of this install: `[gateway].models_file`, else `models.toml` in the config dir."""
    gateway = kitconfig.load(GATEWAY_TABLE)
    configured = str(gateway["models_file"])
    manifest = Path(configured).expanduser() if configured else paths.config_dir() / "models.toml"
    return gwmodels.Store(manifest, Path(), paths.state_dir() / "verified.json")


def pin(plans: list[RolePlan], found: Mapping[str, discovery.Discovery], store: gwmodels.Store) -> list[str]:
    """Pins every assigned model (Ollama digest, GGUF sha256); returns one line per pinned model.

    Raises `ManifestError` when Ollama no longer lists a model or a file cannot be pinned.
    """
    lines = []
    entries = {e.name: e for e in gwmodels.list_entries(store)}
    ollama = [rp.model.name for rp in plans if rp.model and rp.model.kind == "ollama"]
    if ollama:
        for entry in gwmodels.record_ollama(store, sorted(set(ollama)), _Tags(found)):
            lines.append(f"pinned {entry.name} ({entry.source.split('@')[-1][:19]})")
    for rp in plans:
        if not rp.model or rp.model.kind != "gguf":
            continue
        path = Path(rp.model.path).resolve()
        if any(e.file == path for e in entries.values()):
            continue
        name = rp.model.name if rp.model.name not in entries else f"{rp.source.name}-{rp.model.name}"  # type: ignore[union-attr]
        entry = gwmodels.add_local(store, path, name=name)
        entries[entry.name] = entry
        lines.append(f"pinned {entry.name} (sha256 {str(entry.sha256)[:12]})")
        mm = _mmproj(path) if rp.role == "vision" else None
        if mm is not None and not any(e.file == mm.resolve() for e in entries.values()):
            extra = gwmodels.add_local(store, mm, name=f"{name}-mmproj")
            entries[extra.name] = extra
            lines.append(f"pinned {extra.name}")
    return lines


class ApplyError(ValueError):
    """Writing a plan failed; the message says what was rolled back."""


def _check_writable(plans: list[RolePlan], found: Mapping[str, discovery.Discovery]) -> None:
    """Fails before any write when a chosen model cannot be pinned."""
    listed = {m.name for d in found.values() for m in d.models if m.digest}
    for rp in plans:
        if rp.model is None:
            continue
        if rp.model.kind == "gguf" and not Path(rp.model.path).is_file():
            raise ManifestError(f"{rp.model.path} is not a file")
        if rp.model.kind == "ollama" and rp.model.name not in listed and f"{rp.model.name}:latest" not in listed:
            raise ManifestError(f"Ollama does not list {rp.model.name} with a digest; pull it first")


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _put_back(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
    else:
        files.write_atomic(path, data)


def preview(plans: list[RolePlan], pre: Callable[[str], str] | None = None) -> tuple[str, str, Wiring]:
    """`(old config text, new config text, wiring)` for `plans`; `pre` edits the old text first (source changes)."""
    target = paths.config_file()
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    base = pre(old) if pre else old
    wiring = wire(plans, tomllib.loads(base) if base.strip() else {})
    return old, edit_config(base, plans, wiring), wiring  # validates the TOML before anything is written


def apply(
    plans: list[RolePlan], found: Mapping[str, discovery.Discovery], pre: Callable[[str], str] | None = None
) -> list[str]:
    """Pins the models, writes the tuned mode and the config; returns what it did, line by line.

    Everything is planned and validated first (the new TOML must load, every model must be
    pinnable). The writes then run pins, mode, config in that order; if one fails, the manifest
    and the mode file go back to what they were and `ApplyError` says so. The config is written
    last, so a failure never leaves roles that point at models that are not pinned.
    """
    target = paths.config_file()
    old, new, wiring = preview(plans, pre)
    with tempfile.TemporaryDirectory(prefix="hal-apply-") as scratch:
        probe = Path(scratch) / "config.toml"
        probe.write_text(new, encoding="utf-8")
        modelconf.load_roles(probe)
        modelconf.load_sources(probe)
    _check_writable(plans, found)
    store = manifest_store()
    mode_file = paths.config_dir() / "modes" / f"{tunedmode.MODE_NAME}.ini"
    saved = {store.manifest: _read_bytes(store.manifest), mode_file: _read_bytes(mode_file)}
    done: list[str] = []
    try:
        done = pin(plans, found, store)
        if wiring.presets:
            done.append(f"wrote {tunedmode.write(wiring.presets, mode_file.parent)}")
        backup = confmigrate.apply(target, new)
    except plain_errors() as exc:
        changed = [p for p, data in saved.items() if _read_bytes(p) != data]
        for path, data in saved.items():
            _put_back(path, data)
        undone = f"; rolled back the pins and files this run added ({len(changed)})" if changed else ""
        raise ApplyError(f"nothing was written ({exc}){undone}") from exc
    done.append(f"wrote {target}" + (f" (backup {backup.name})" if old else ""))
    return [*done, *wiring.notes]


def plain_errors() -> tuple[type[Exception], ...]:
    """Exceptions a command reports as a one-line failure."""
    return (kitconfig.ConfigError, ManifestError, ValueError, OSError)


def assignment_status() -> tuple[bool, str]:
    """Whether a chat model is assigned, and one line saying so or naming the next step.

    Never raises: an unreadable config gives `(False, reason)`.
    """
    try:
        sources = modelconf.load_sources()
        roles = modelconf.load_roles()
    except kitconfig.ConfigError as exc:
        return False, f"the models config is invalid: {exc}"
    chat = roles.get("chat")
    if chat is not None:
        return True, f"chat model: {chat.source}/{chat.model}"
    if not sources:
        return False, (
            "no model assigned; point Hal at yours: hal models add-source ollama http://127.0.0.1:11434"
            " (or gguf DIR), then hal tune --auto --write"
        )
    return False, "no model assigned; run: hal tune --auto --write"

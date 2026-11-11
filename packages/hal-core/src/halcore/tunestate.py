"""The state model of the interactive `hal tune`: drafts per role, live fit and the config diff.

It holds no terminal code, so the screen (`halcore.tuneui`) and the tests drive the same object.
Every change goes into a draft; nothing touches the config until `write`, which builds
`modelplan.RolePlan`s from the drafts and hands them to `modelplan.apply`, so pinning, rollback
and comment-preserving edits are the ones `hal tune --write` uses.

Design decisions:

- A draft with `manual` false is re-planned from the hardware on every change (context 0 means
  "decided by tuning"); only Auto (a field's Auto button, Auto-tune, a preset) makes it so.
  Setting a context, a KV type, GPU layers or experts by hand makes the draft manual: the plan is
  then `tuning.place` with exactly those numbers, checked against the whole card rather than the
  auto-tune guidance, and the role is written with `auto = false`. Nothing the user set is
  changed for them; a setting that spills to RAM is allowed and shown as such.
- A context set by hand may go up to the model's own maximum; `[models] context_cap`, off by
  default, only limits what auto-tune picks.
- A model the server sizes (an OpenAI-compatible server such as vLLM) cannot be planned, but its
  context can still be declared, so the gateway reports the window the server really has.
- Source changes are kept as edits to the config text and applied with the roles, so the diff
  and the write see one change set.
- GPU layers set by hand (`Draft.gpu_layers`, None = decided by tuning) and, for a
  mixture-of-experts GGUF, layers whose experts stay in RAM (`Draft.cpu_moe`) work the same way.
  A layer count equal to the model's layers means all of them, written as `-1` like a tuned plan.
- `snapshot` and `restore` copy every draft and the source edits, so the screen can undo any
  change, including a preset.
- `hal/main` and `hal/agent` (what Claude Code and other harnesses ask for) point at aliases,
  `[gateway] main` and `agent`; `harness` holds them and a write sets those keys.
- Named roles (`modelconf.is_custom`) are added empty and written once they have a model.
  Removing one is a config edit kept with the source edits, so the diff shows it and undo
  brings it back. Auto-tune and presets leave named roles alone: they are the user's setups.
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from halgateway.config import GATEWAY_TABLE
from halkit import config, discovery, effort, modelconf, tomledit, tuning
from halkit import hardware as hwmod
from halkit.hardware import Hardware
from halkit.modelinfo import ModelInfo

from . import modelplan, tunepresets

CONTEXT_PRESETS = (8192, 16384, 32768, 65536, 131072, 262144)
KV_OPTIONS = modelconf.KV_CHOICES
OPTIONAL_ROLES = ("writer",)  # empty by design: falls back to chat
DEFAULT_EFFORT = "default"  # the word the screens show for "no effort set"
MAX_DRAFT_TOKENS = 16  # llama.cpp drafts at most 16 tokens per step for a block drafter
DEFAULT_HARNESS = {"main": "hal-code", "agent": "hal-code"}  # `[gateway] main` and `agent` defaults
ROLE_KINDS = {"code": "coding model", "vision": "vision model", "embed": "embedding model", "classifier": "safety classifier"}


@dataclass
class Draft:
    """The settings of one role while the screen is open."""

    source: str = ""
    model: str = ""
    context: int = 0  # 0 = decided by tuning
    kv_type: str = ""  # "" = the `[models]` default
    effort: str = ""
    temperature: float = modelconf.UNSET_TEMPERATURE
    family: str = ""
    manual: bool = False
    gpu_layers: int | None = None  # None = decided by tuning; else 0 to the model's layer count
    cpu_moe: int | None = None  # None = decided by tuning; else layers whose experts stay in RAM
    draft: str = ""  # speculative decoding: "" = the best draft found, "off", or a draft file
    draft_max: int = 0  # tokens drafted per step; 0 = the default for the draft kind


@dataclass(frozen=True)
class Snapshot:
    """A copy of everything an edit can change, for undo."""

    drafts: dict[str, Draft]
    sources: dict[str, modelconf.Source]
    found: dict[str, discovery.Discovery]
    source_edits: list[Callable[[str], str]]
    added: frozenset[str]
    removed: frozenset[str]
    named: frozenset[str]
    hw: Hardware
    preset: tunepresets.Preset | None
    harness: dict[str, str]


@dataclass(frozen=True)
class Fit:
    """What a draft needs and where it runs: the numbers behind the fit line."""

    status: str  # gpu, hybrid, cpu, does-not-fit, server (size unknown or remote) or empty
    context: int = 0
    kv_type: str = ""
    gpu_layers: int = 0
    used_mib: int = 0  # VRAM the model takes: weights there, cache and runtime overhead
    budget_mib: int = 0  # the guidance: what auto-tune plans within
    estimated: bool = False
    notes: tuple[str, ...] = ()
    total_mib: int = 0  # the cards' total memory: the limit of a setting made by hand
    ram_mib: int = 0  # weights (and cache) in system RAM
    cpu_moe: int = 0
    draft: str = ""  # the draft file in use, "" for none
    draft_max: int = 0

    @property
    def above_guidance(self) -> bool:
        """True when the model is on the GPU but past the auto-tune guidance budget."""
        return self.status in ("gpu", "hybrid") and 0 < self.budget_mib < self.used_mib

    @property
    def ok(self) -> bool:
        """True unless the model does not fit."""
        return self.status != "does-not-fit"


def _draft_of(role: modelconf.Role) -> Draft:
    layers = role.gpu_layers if not role.auto and role.gpu_layers >= 0 else None
    moe = role.cpu_moe if not role.auto and role.cpu_moe > 0 else None
    return Draft(role.source, role.model, 0 if role.auto else role.context, role.kv_type, role.effort,
                 role.temperature, role.family, manual=not role.auto, gpu_layers=layers, cpu_moe=moe,
                 draft=role.draft, draft_max=role.draft_max)  # fmt: skip


class TuneSession:
    """Hardware, sources, discovered models and one `Draft` per role."""

    def __init__(
        self, hw: Hardware, sources: Mapping[str, modelconf.Source], found: Mapping[str, discovery.Discovery],
        roles: Mapping[str, modelconf.Role], limits: Mapping[str, object],
        discover: Callable[[modelconf.Source], discovery.Discovery] = discovery.discover,
        harness: Mapping[str, str] | None = None,
    ) -> None:
        self.hw = hw
        self.real_hw = hw  # `hw` is what plans use; a preset swaps it, this is the machine
        self.preset: tunepresets.Preset | None = None
        self.sources = dict(sources)
        self.found = dict(found)
        self.limits = limits
        self._discover = discover
        self._original = {r: _draft_of(cfg) for r, cfg in roles.items()}
        self.drafts: dict[str, Draft] = {r: replace(d) for r, d in self._original.items()}
        self._roles = dict(roles)
        self._source_edits: list[Callable[[str], str]] = []
        self._added: set[str] = set()
        self._removed: set[str] = set()
        self._named: set[str] = {r for r in roles if modelconf.is_custom(r)}
        self._harness_original = dict(harness or DEFAULT_HARNESS)
        self.harness = dict(self._harness_original)

    # --- models and fit ---------------------------------------------------------------------

    def models(self) -> list[ModelInfo]:
        """Every discovered model a role can use, in source order (models tune made itself are left out)."""
        return [m for d in self.found.values() for m in d.models if not modelplan.is_derived(m.name)]

    def find(self, source: str, name: str) -> ModelInfo | None:
        """The discovered model `name` of `source`, or None."""
        return modelplan._find(self.found, source, name)  # noqa: SLF001 - same package

    def model_of(self, role: str) -> ModelInfo | None:
        """The model a role's draft names, when discovery lists it."""
        d = self.drafts.get(role)
        return self.find(d.source, d.model) if d else None

    def _role_cfg(self, role: str) -> modelconf.Role | None:
        d = self.drafts.get(role)
        if d is None:
            return None
        return modelconf.Role(role, d.source, d.model, d.context, d.kv_type, effort=d.effort, temperature=d.temperature,
                              family=d.family, draft=d.draft, draft_max=d.draft_max)  # fmt: skip

    def plan(self, role: str) -> tuning.Plan | None:
        """The plan for a role's draft: auto from the hardware, or the user's numbers placed on it.

        None for a model Hal cannot size (the server decides its memory).
        """
        d, model = self.drafts.get(role), self.model_of(role)
        if d is None or model is None or not self.sized(model):
            return None
        if d.manual and d.context > 0:
            kv = d.kv_type if d.kv_type in tuning.KV_TYPES else self._auto_kv(model)
            draft = modelplan.draft_of(model, self._role_cfg(role))
            return tuning.place(
                model, self.hw, kv_type=kv, context=d.context, gpu_layers=d.gpu_layers,
                cpu_moe=d.cpu_moe if model.is_moe else None, headroom_override_mib=self._headroom(),
                draft_cost_mib=draft.vram_mib if draft else 0,
            )  # fmt: skip
        return modelplan._plan(model, self.hw, self._role_cfg(role), self.limits)  # noqa: SLF001 - same package

    def _headroom(self) -> int:
        return int(self.limits["headroom_mib"])  # type: ignore[call-overload]  # validated config

    def ceiling(self, role: str) -> int:
        """The largest context the role's model takes by hand: its own maximum, or what the server serves."""
        model = self.model_of(role)
        if model is None:
            return tuning.UNKNOWN_NATIVE_CONTEXT
        return tuning.ceiling(model)

    @staticmethod
    def sized(model: ModelInfo) -> bool:
        """True when Hal can size the model (a local model with a known weight size)."""
        return bool(model.size_bytes) and model.kind not in ("anthropic", "openai-cloud")

    def layers_of(self, role: str) -> tuple[int, str]:
        """`(layer count, reason)`: the count when layers can be set by hand, else 0 and why not."""
        d, model = self.drafts.get(role), self.model_of(role)
        if d is None or model is None:
            return 0, "choose a model first"
        if not self.sized(model):
            return 0, "the server decides the offload of this model"
        if not model.layers:
            return 0, "the layer count of this model is unknown"
        if not self.hw.gpus:
            return 0, "no GPU in this plan: every layer runs on the CPU"
        return int(model.layers), ""

    def experts_of(self, role: str) -> tuple[int, str]:
        """`(layer count, reason)` when experts can be kept in RAM by hand, else 0 and why not."""
        total, reason = self.layers_of(role)
        model = self.model_of(role)
        if not total or model is None:
            return 0, reason
        if not model.is_moe:
            return 0, "not a mixture-of-experts GGUF model"
        return total, ""

    def _auto_kv(self, model: ModelInfo) -> str:
        plan = modelplan._plan(model, self.hw, None, self.limits)  # noqa: SLF001 - same package
        return plan.kv_type if plan else "f16"

    def fit(self, role: str) -> Fit:
        """The fit of a role; status `empty` without a model, `server` when Hal cannot size it."""
        d = self.drafts.get(role)
        if d is None or not d.model:
            return Fit("empty")
        if self.model_of(role) is None:
            return Fit("missing", notes=(f"{d.model} is not listed by {d.source}",))
        try:
            plan = self.plan(role)
        except ValueError as exc:
            return Fit("missing", notes=(str(exc),))
        if plan is None:
            model = self.model_of(role)
            served = d.context or (model.context_served or model.context_native or 0 if model else 0)
            why = "the server decides offload; the context is the window it serves" if served else "the server decides context and offload"
            return Fit("server", context=served, notes=(why,))
        draft = modelplan.draft_of(self.model_of(role), self._role_cfg(role))  # type: ignore[arg-type]  # planned above
        return Fit(plan.status, plan.context, plan.kv_type, plan.gpu_layers, plan.vram_mib, plan.budget_mib,
                   plan.estimated, plan.reasoning, plan.total_mib, plan.ram_mib, plan.cpu_moe,
                   draft.path if draft else "", draft.draft_max if draft else 0)  # fmt: skip

    def try_context(self, role: str, tokens: int) -> Fit:
        """The fit `role` would have with `tokens` of context; the draft is not changed."""
        return self._try(role, lambda: self.set_context(role, tokens))

    def try_layers(self, role: str, layers: int | None) -> Fit:
        """The fit `role` would have with this many GPU layers; the draft is not changed."""
        return self._try(role, lambda: self.set_gpu_layers(role, layers))

    def try_experts(self, role: str, layers: int | None) -> Fit:
        """The fit `role` would have with the experts of this many layers in RAM; the draft is not changed."""
        return self._try(role, lambda: self.set_cpu_moe(role, layers))

    def try_kv(self, role: str, kv_type: str) -> Fit:
        """The fit `role` would have with this KV type; the draft is not changed."""
        return self._try(role, lambda: self.set_kv(role, kv_type))

    def _try(self, role: str, change: Callable[[], None]) -> Fit:
        saved = self.drafts.get(role)
        if saved is None:
            return Fit("empty")
        self.drafts[role] = replace(saved)
        try:
            change()
            return self.fit(role)
        except ValueError as exc:
            return Fit("missing", notes=(str(exc),))
        finally:
            self.drafts[role] = saved

    # --- edits ------------------------------------------------------------------------------

    def choose(self, role: str, model: ModelInfo) -> None:
        """Gives `role` this model and returns its settings to automatic tuning."""
        old = self.drafts.get(role, Draft())
        self.drafts[role] = Draft(model.source, model.name, effort=old.effort, temperature=old.temperature)
        self._fix_effort(role)

    def set_context(self, role: str, tokens: int) -> None:
        """Sets the context by hand; raises `ValueError` below the floor or past the model's own maximum.

        The KV type and any GPU layers or experts the user set stay as they are; a context that
        no longer fits the card spills to RAM rather than being refused.
        """
        d, model = self._need(role)
        ceiling = self.ceiling(role)
        if tokens < tuning.MIN_CONTEXT:
            raise ValueError(f"the context must be at least {tuning.MIN_CONTEXT} tokens")
        if tokens > ceiling:
            raise ValueError(f"{model.name} takes at most {ceiling} tokens")
        d.context, d.manual = tuning.round_context(tokens), True
        if d.kv_type in ("", "auto") and self.sized(model):
            d.kv_type = self._auto_kv(model)

    def set_kv(self, role: str, kv_type: str) -> None:
        """Sets the KV cache type by hand; `auto` lets tuning choose the type again.

        Choosing a type by hand keeps the context: the current one (tuned or set) becomes the
        user's, so changing the cache never moves the context under them. `auto` changes only
        the type; the context and offload stay as they are.
        """
        d, _ = self._need(role)
        if kv_type not in KV_OPTIONS:
            raise ValueError(f"KV type must be one of {', '.join(KV_OPTIONS)}")
        if kv_type == "auto":
            d.kv_type = ""
            return
        self._pin_context(role)
        d.kv_type, d.manual = kv_type, True

    def auto_context(self, role: str) -> None:
        """Returns the context (and the cache type and offload) of `role` to automatic tuning."""
        d, _ = self._need(role)
        d.manual, d.context, d.kv_type, d.gpu_layers, d.cpu_moe = False, 0, "", None, None

    def _pin_context(self, role: str) -> None:
        """Makes the current context the user's, so a later edit leaves it alone."""
        d = self.drafts[role]
        if d.context <= 0:
            current = self.fit(role)
            d.context = current.context if current.context >= tuning.MIN_CONTEXT else tuning.MIN_CONTEXT
        model = self.model_of(role)
        if d.kv_type in ("", "auto") and model is not None and self.sized(model):
            d.kv_type = self._auto_kv(model)

    def set_gpu_layers(self, role: str, layers: int | None) -> None:
        """Puts `layers` layers on the GPU by hand; None returns the layers to tuning.

        Raises `ValueError` when the count is out of range or cannot be set for this model.
        """
        d, model = self._need(role)
        if layers is None:
            d.gpu_layers = None
            return
        total, reason = self.layers_of(role)
        if not total:
            raise ValueError(reason)
        if not 0 <= layers <= total:
            raise ValueError(f"{model.name} has {total} layers; choose 0 to {total}")
        self._pin_context(role)
        d.gpu_layers, d.manual = layers, True

    def set_cpu_moe(self, role: str, layers: int | None) -> None:
        """Keeps the routed experts of `layers` layers in RAM by hand; None returns them to tuning.

        Raises `ValueError` when the model is not a mixture-of-experts GGUF or the count is out of range.
        """
        d, model = self._need(role)
        if layers is None:
            d.cpu_moe = None
            return
        total, reason = self.experts_of(role)
        if not total:
            raise ValueError(reason)
        if not 0 <= layers <= total:
            raise ValueError(f"{model.name} has {total} layers; choose 0 to {total}")
        self._pin_context(role)
        d.cpu_moe, d.manual = layers, True

    def snapshot(self) -> Snapshot:
        """A copy of the drafts, sources and source edits, for `restore`."""
        return Snapshot(
            copy.deepcopy(self.drafts), dict(self.sources), dict(self.found), list(self._source_edits),
            frozenset(self._added), frozenset(self._removed), frozenset(self._named), self.hw, self.preset,
            dict(self.harness),
        )  # fmt: skip

    def restore(self, snap: Snapshot) -> None:
        """Puts back everything `snapshot` copied."""
        self.drafts = copy.deepcopy(snap.drafts)
        self.sources, self.found = dict(snap.sources), dict(snap.found)
        self._source_edits = list(snap.source_edits)
        self._added, self._removed, self._named = set(snap.added), set(snap.removed), set(snap.named)
        self.hw, self.preset = snap.hw, snap.preset
        self.harness = dict(snap.harness)

    def drafts_of(self, role: str) -> tuple[tuple[str, int], ...]:
        """The draft models found beside the role's GGUF model, best first; empty for other kinds."""
        model = self.model_of(role)
        return model.drafts if model is not None and model.kind == "gguf" else ()

    def set_draft(self, role: str, draft: str) -> None:
        """Speculative decoding: "" for the best draft found, `off` for none, or one of `drafts_of`."""
        d, model = self._need(role)
        if draft not in ("", tuning.DRAFT_OFF) and draft not in dict(self.drafts_of(role)):
            raise ValueError(f"{draft} is not a draft model found beside {model.name}")
        d.draft = draft

    def set_draft_max(self, role: str, tokens: int) -> None:
        """Tokens drafted per step (0 = the default for the draft kind); raises `ValueError` outside 0 to 16."""
        d, _ = self._need(role)
        if not 0 <= tokens <= MAX_DRAFT_TOKENS:
            raise ValueError(f"draft tokens must be 0 (default) to {MAX_DRAFT_TOKENS}")
        d.draft_max = tokens

    def set_effort(self, role: str, level: str) -> None:
        """Sets the default effort (`""` = the model's default); refused when the model cannot use the level."""
        d, _ = self._need(role)
        if level in ("", DEFAULT_EFFORT):
            d.effort = ""
            return
        options, reason = self.effort_options(role)
        if level not in modelconf.EFFORT_CHOICES:
            raise ValueError(f"effort must be one of {', '.join(modelconf.EFFORT_CHOICES)}")
        if level not in options:
            raise ValueError(reason or f"effort {level} is not available")
        d.effort = level

    def set_temperature(self, role: str, value: float) -> None:
        """Sets the temperature (-1 = the model's own default)."""
        d, _ = self._need(role)
        if value != modelconf.UNSET_TEMPERATURE and not 0 <= value <= modelconf.MAX_TEMPERATURE:
            raise ValueError(f"temperature must be 0 to {modelconf.MAX_TEMPERATURE:g}, or -1 for the model's default")
        d.temperature = value

    def clear(self, role: str) -> None:
        """Removes the role's model from the draft (the config table is kept until it is replaced)."""
        self.drafts.pop(role, None)

    def _need(self, role: str) -> tuple[Draft, ModelInfo]:
        d, model = self.drafts.get(role), self.model_of(role)
        if d is None or model is None:
            raise ValueError(f"role {role} has no model yet; choose one first")
        return d, model

    def effort_options(self, role: str) -> tuple[tuple[str, ...], str]:
        """`(levels, reason)`: the levels the model supports, or none and a one-line reason."""
        d, model = self.drafts.get(role), self.model_of(role)
        if d is None or model is None:
            return (), "no model chosen"
        return effort.levels_for(effort.capability(d.family or model.family, model.name))

    def _fix_effort(self, role: str) -> None:
        d = self.drafts[role]
        if d.effort and d.effort not in self.effort_options(role)[0]:
            d.effort = ""

    def auto_tune(self, role: str | None = None) -> list[str]:
        """Re-tunes one role, or every role; an empty role gets the best local model. Returns notes."""
        notes = []
        for name in ([role] if role else [r for r in modelconf.ROLES if r not in OPTIONAL_ROLES or r in self.drafts]):
            d = self.drafts.get(name)
            model = self.model_of(name)
            if model is None:
                rp = modelplan.choose(name, self.found, self.sources, self.hw, self.limits, {})
                if rp.model is None:
                    notes.append(f"{name}: " + "; ".join(rp.notes))
                    continue
                self.choose(name, rp.model)
                notes.append(f"{name}: chose {rp.model.name}")
            elif d is not None:
                self.auto_context(name)
                notes.append(f"{name}: tuned for this hardware")
        return notes

    def apply_preset(self, preset: tunepresets.Preset) -> list[str]:
        """Tunes every role against the preset's VRAM budget; returns one note per role.

        A role keeps its model when it runs fully on the planned hardware (on the CPU for a
        CPU preset); otherwise it gets the best discovered model that fits. A role with no
        fitting model is left as it was and its note says how to get one.
        """
        self.hw, self.preset = tunepresets.hardware_for(preset, self.real_hw), preset
        keep = "gpu" if self.hw.gpus else "cpu"
        notes = []
        for role in [r for r in modelconf.ROLES if r not in OPTIONAL_ROLES or r in self.drafts]:
            if self.fit(role).status in (keep, "server"):
                self.auto_context(role)
                notes.append(f"{role}: {self.drafts[role].model} fits {preset.label}")
                continue
            rp = modelplan.choose(role, self.found, self.sources, self.hw, self.limits, {})
            if rp.model is None:
                kind = ROLE_KINDS.get(role, "model")
                tag = tunepresets.recommended(preset, role, self.real_hw)
                pull = f"`ollama pull {tag}`" if tag else f"a smaller {kind} with `ollama pull <model>`"
                notes.append(
                    f"{role}: no discovered model fits {preset.label}. Pull {pull} "
                    "or add a source, then run Quick setup again"
                )
                continue
            self.choose(role, rp.model)
            notes.append(f"{role}: chose {rp.model.name} for {preset.label}")
        return notes

    # --- hal/main and hal/agent -------------------------------------------------------------

    def alias_names(self) -> list[str]:
        """The aliases `hal/main` and `hal/agent` may serve: one per role, built-in and named."""
        return [f"hal-{role}" for role in self.role_names()]

    def set_harness(self, key: str, alias: str) -> None:
        """Points `hal/main` (`key` = `main`) or `hal/agent` (`agent`) at `alias`; raises `ValueError`."""
        if key not in DEFAULT_HARNESS:
            raise ValueError(f"no setting {key!r}; use main or agent")
        if alias not in self.alias_names():
            raise ValueError(f"{alias} is not one of {', '.join(self.alias_names())}")
        self.harness[key] = alias

    def _harness_edit(self, text: str) -> str:
        changed = {k: v for k, v in self.harness.items() if v != self._harness_original.get(k)}
        return tomledit.set_keys(text, "gateway", changed) if changed else text

    # --- named roles ------------------------------------------------------------------------

    def role_names(self) -> list[str]:
        """The built-in roles, then the named ones, in the order the screens list them."""
        return [*modelconf.ROLES, *sorted(self._named)]

    def add_role(self, name: str) -> str:
        """Adds an empty named role; raises `ValueError` for a bad or taken name. Returns the name."""
        name = name.strip().lower()
        why = modelconf.check_role_name(name)
        if why is None and name in self._named:
            why = f"{name!r} is already a role name"
        if why:
            raise ValueError(why)
        self._named.add(name)
        return name

    def remove_role(self, name: str) -> None:
        """Removes a named role, and on write its config table and gateway alias."""
        if not modelconf.is_custom(name):
            raise ValueError(f"{name} is a built-in role; choose another model or clear it instead")
        if name not in self._named:
            raise ValueError(f"no role named {name!r}")
        self._named.discard(name)
        self.drafts.pop(name, None)
        if name in self._original:
            self._source_edits.append(lambda text, n=name: modelplan.remove_role(text, n))

    # --- sources ----------------------------------------------------------------------------

    def add_source(self, kind: str, location: str, name: str = "") -> str:
        """Adds a source to the draft and discovers its models; returns its name. Raises `ValueError` on bad input."""
        from . import modelscmd  # noqa: PLC0415 - modelscmd imports this package's engine at load

        taken = set(self.sources)
        src_name = modelscmd.source_name(kind, taken, name or None)
        if src_name in taken:
            raise ValueError(f"source {src_name!r} exists")
        values: dict[str, object] = {"kind": kind}
        if kind in modelconf.URL_KINDS:
            values["url"] = location or modelconf.DEFAULT_URLS[kind]
        elif kind == "gguf":
            if not location:
                raise ValueError("a gguf source needs a folder")
            values["path"] = location
        elif kind in modelconf.KEY_FILE_KINDS:
            if not location:
                raise ValueError(f"a {kind} source needs the path of a key file")
            values["api_key_file"] = location
        self._source_edits.append(lambda text, n=src_name, v=values: tomledit.set_table(text, f"models.sources.{n}", v))
        try:
            src = self._validated_source(src_name)
        except config.ConfigError as exc:
            self._source_edits.pop()
            raise ValueError(str(exc)) from exc
        self.sources[src_name] = src
        self.found[src_name] = self._discover(src)
        self._added.add(src_name)
        self._removed.discard(src_name)
        return src_name

    def _validated_source(self, name: str) -> modelconf.Source:
        import tempfile  # noqa: PLC0415 - only this check needs it
        from pathlib import Path

        text = ""
        for edit in self._source_edits:
            text = edit(text)
        with tempfile.TemporaryDirectory(prefix="hal-tune-") as scratch:
            probe = Path(scratch) / "config.toml"
            probe.write_text(text, encoding="utf-8")
            return modelconf.load_sources(probe)[name]

    def remove_source(self, name: str) -> None:
        """Removes a source from the draft; refused while a role's draft uses it."""
        if name not in self.sources:
            raise ValueError(f"no source named {name!r}")
        users = [r for r, d in self.drafts.items() if d.source == name]
        if users:
            raise ValueError(f"roles {', '.join(users)} use {name!r}; choose other models first")
        self._source_edits.append(lambda text, n=name: tomledit.remove_table(text, f"models.sources.{n}"))
        del self.sources[name]
        self.found.pop(name, None)
        self._removed.add(name)
        self._added.discard(name)

    def _pre(self, text: str) -> str:
        for edit in self._source_edits:
            text = edit(text)
        return self._harness_edit(text)

    # --- changes and writing ----------------------------------------------------------------

    def changed_roles(self) -> list[str]:
        """Roles whose draft differs from the config, in role order."""
        return [r for r in self.role_names() if r in self.drafts and self.drafts[r] != self._original.get(r)]

    def has_changes(self) -> bool:
        """True when a write would change the config."""
        return bool(self.changed_roles() or self._source_edits or self.harness != self._harness_original)

    def role_plans(self) -> list[modelplan.RolePlan]:
        """`RolePlan`s of the changed roles, ready for `modelplan.apply`."""
        out = []
        for role in self.changed_roles():
            d, model = self.drafts[role], self.model_of(role)
            if model is None:
                raise ValueError(f"{role}: {d.model} is not listed by {d.source}; choose another model")
            plan = self.plan(role)
            if plan is not None and plan.status == "does-not-fit":
                raise ValueError(f"{role}: {model.name} does not fit this machine; choose a smaller model or context")
            rp = modelplan.RolePlan(role, self.sources[d.source], model, plan, assigned=role in self._original, manual=d.manual)
            if plan is None and d.manual and d.context:
                rp.declared_context = d.context
            rp.settings = {"effort": d.effort, "temperature": d.temperature, "draft": d.draft, "draft_max": d.draft_max}
            rp.draft = modelplan.draft_of(model, self._role_cfg(role))
            if d.family:
                rp.settings["family"] = d.family
            out.append(rp)
        return out

    def diff(self) -> str:
        """A unified diff of the config as it is and as a write would leave it; empty when unchanged."""
        old, new, wiring = modelplan.preview(self.role_plans(), self._pre)
        lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "config.toml", "config.toml (new)", lineterm="", n=1))
        return "\n".join(lines + [f"note: {n}" for n in wiring.notes])

    def write(self) -> list[str]:
        """Writes the changes through `modelplan.apply`; returns what it did. Raises on failure with nothing written."""
        lines = modelplan.apply(self.role_plans(), self.found, self._pre)
        self._original = {r: replace(d) for r, d in self.drafts.items()}
        self._source_edits.clear()
        self._harness_original = dict(self.harness)
        self._added.clear()
        self._removed.clear()
        return lines


def open_state(
    *, hw: hwmod.Hardware | None = None, discover: Callable | None = None, say: Callable[[str], None] | None = None
) -> TuneSession:
    """Detects the hardware, reads the config and lists every source's models."""
    say = say or (lambda _: None)
    say("Detecting hardware and listing models...")
    sources, roles, limits = modelconf.load_sources(), modelconf.load_roles(), modelconf.limits()
    gateway = config.load(GATEWAY_TABLE)
    harness = {key: str(gateway[key]) or DEFAULT_HARNESS[key] for key in DEFAULT_HARNESS}
    hw = hw or hwmod.detect()
    kwargs = {"discover": discover} if discover else {}
    found = modelplan.survey(sources, **kwargs)
    return TuneSession(hw, sources, found, roles, limits, harness=harness, **kwargs)

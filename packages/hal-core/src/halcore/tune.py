"""`hal tune`: detect the hardware, look at the models you have, and work out the settings.

    hal tune [--auto] [--write] [--json] [--role ROLE ...] [--reassign] [--hardware FILE]

Without `--write` nothing is changed; the plan shows, per role, the model, the context length,
the KV cache type and how many layers go on the GPU, with the numbers behind each choice.
`--write` pins the chosen models, writes `[models.roles.*]` and the gateway wiring to the config
(with a backup), and generates the llama.cpp mode `tuned` for GGUF roles. Nothing is downloaded.
On a terminal, a bare `hal tune` (or `-i`) opens the interactive screen instead: the full-screen
app (`halcore.tuneapp`) where the terminal can run it, else the line screen (`halcore.tuneui`);
off a terminal, or with any flag, it shows the `--auto` plan.

Exit status: 0 done, 1 error, 2 usage, 3 nothing to tune (no source, or no usable model).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from halkit import config as kitconfig
from halkit import hardware, modelconf, paths

from . import modelplan

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOTHING = 0, 1, 2, 3
log = logging.getLogger(__name__)
ADD_SOURCE_HINT = "hal models add-source ollama http://127.0.0.1:11434"


def describe(hw: hardware.Hardware) -> str:
    """One line per fact about the hardware."""
    gpus = [f"{g.name} {g.total_mib} MiB ({g.free_mib} free)" + (f" cc {g.compute_cap}" if g.compute_cap else "") for g in hw.gpus]
    lines = [
        "GPU: " + ("; ".join(gpus) if gpus else "none (CPU only)"),
        f"RAM: {hw.ram_total_mib} MiB ({hw.ram_free_mib} free), CPU threads: {hw.cpu_threads}" + (", WSL2" if hw.wsl2 else ""),
    ]
    return "\n".join(lines + [f"note: {n}" for n in hw.notes])


def render_text(hw: hardware.Hardware, plans: list[modelplan.RolePlan], errors: dict[str, str]) -> str:
    """The human-readable plan."""
    out = [describe(hw), ""]
    for name, error in errors.items():
        out.append(f"source {name}: {error}")
    out.append(f"{'role':<8}{'model':<34}{'status':<14}{'context':>8}  {'kv':<5}{'gpu layers'}")
    for rp in plans:
        if rp.model is None:
            out.append(f"{rp.role:<8}{'-':<34}{'no model':<14}")
        elif rp.plan is None:
            out.append(f"{rp.role:<8}{rp.source.name + '/' + rp.model.name:<34}{'server':<14}")  # type: ignore[union-attr]
        else:
            p = rp.plan
            layers = "all" if p.gpu_layers < 0 else str(p.gpu_layers)
            label = f"{rp.source.name}/{rp.model.name}"  # type: ignore[union-attr]
            out.append(f"{rp.role:<8}{label:<34}{p.status:<14}{p.context:>8}  {p.kv_type:<5}{layers}")
        for note in rp.notes:
            out.append(f"        - {note}")
        if rp.plan:
            out += [f"        - {line}" for line in rp.plan.reasoning]
        if rp.locked:
            out.append("        - auto = false: your context, kv_type and gpu_layers are kept")
    return "\n".join(out)


def next_step(sources: dict, plans: list[modelplan.RolePlan]) -> str:
    """The exact command to run next, or an empty string."""
    if not sources:
        return ADD_SOURCE_HINT
    if not any(rp.model for rp in plans):
        return "hal models list   # no model was found in your sources"
    return ""


def is_interactive() -> bool:
    """True when stdin and stdout are both a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_screen(hw: hardware.Hardware | None = None) -> int:
    """Opens the interactive screen: the full-screen app when the terminal can run it, else the line screen.

    When the app fails before it has drawn anything, one notice is printed and the line screen
    opens on the same state, so nothing the user did is lost.
    """
    from . import tunestate  # noqa: PLC0415 - the screens load only when one opens
    from .tui import fallback
    from .tui.theme import UI_TABLE, load_look

    try:
        look = load_look(kitconfig.load(UI_TABLE), stream=sys.stdout)
    except kitconfig.ConfigError as exc:
        log.warning("ignoring [ui] settings: %s", exc)
        look = load_look(stream=sys.stdout)
    ok, reason = fallback.can_run_textual(sys.stdin, sys.stdout, os.environ, look.interface)
    if not ok:
        from . import tuneui  # noqa: PLC0415 - prompt_toolkit loads only for the line screen

        return tuneui.run_interactive(hw=hw)
    print("Detecting hardware and listing models...")
    try:
        state = tunestate.open_state(hw=hw)
    except (ValueError, OSError, *modelplan.plain_errors()) as exc:
        print(f"hal tune: {exc}", file=sys.stderr)
        return EXIT_ERROR
    from . import tuneapp  # noqa: PLC0415 - Textual loads only for the full screen

    app = tuneapp.TuneApp(state, look)
    try:
        status = app.run(mouse=look.mouse)
    except Exception as exc:  # noqa: BLE001 - a start failure falls back to the line screen; later ones re-raise
        if app.mounted:
            raise
        print(fallback.notice(str(exc) or type(exc).__name__), file=sys.stderr)
        from . import tuneui  # noqa: PLC0415 - prompt_toolkit loads only for the line screen

        return tuneui.run_line_screen(state, look)
    for line in app.written:
        print(line)
    if app.left_unsaved:
        print("Left without writing: the config is unchanged.")
    return status if isinstance(status, int) else EXIT_OK


def main(argv: list[str]) -> int:
    """Entry point of `hal tune`."""
    ap = argparse.ArgumentParser(prog="hal tune", description="Detect hardware and tune the model settings.")
    ap.add_argument("--auto", action="store_true", help="choose and plan without questions")
    ap.add_argument("--write", action="store_true", help="pin the models and write the config")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    ap.add_argument("--role", action="append", choices=modelconf.ROLES, default=[], help="only this role (repeatable)")
    ap.add_argument("--reassign", action="store_true", help="choose again for roles that already have a model")
    ap.add_argument("--hardware", metavar="FILE", help="plan for the machine described in this JSON file")
    ap.add_argument("-i", "--interactive", action="store_true", help="open the interactive screen (the default on a terminal with no flags)")
    args = ap.parse_args(argv)
    if args.interactive or (not argv and is_interactive()):
        try:
            hw = hardware.from_dict(json.loads(Path(args.hardware).read_text(encoding="utf-8"))) if args.hardware else None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"hal tune: {exc}", file=sys.stderr)
            return EXIT_ERROR
        return run_screen(hw)
    try:
        sources = modelconf.load_sources()
        roles = modelconf.load_roles()
        limits = modelconf.limits()
        hw = hardware.from_dict(json.loads(Path(args.hardware).read_text(encoding="utf-8"))) if args.hardware else hardware.detect()
    except (OSError, ValueError, json.JSONDecodeError, *modelplan.plain_errors()) as exc:
        print(f"hal tune: {exc}", file=sys.stderr)
        return EXIT_ERROR
    found = modelplan.survey(sources)
    plans = modelplan.build(roles, sources, found, hw, limits, only=tuple(args.role), reassign=args.reassign)
    errors = {n: d.error for n, d in found.items() if d.error}
    step = next_step(sources, plans)
    written: list[str] | None = None
    if args.write and not step:
        try:
            written = modelplan.apply(plans, found)
        except modelplan.plain_errors() as exc:
            print(f"hal tune: {exc}", file=sys.stderr)
            return EXIT_ERROR
    if args.json:
        doc = {"hardware": hw.to_dict(), "sources": {n: {"models": len(d.models), "error": d.error} for n, d in found.items()},
               "roles": [rp.to_dict() for rp in plans], "written": written, "next_step": step, "config": str(paths.config_file())}
        print(json.dumps(doc, indent=2))
    else:
        print(render_text(hw, plans, errors))
        if written is not None:
            print("\n" + "\n".join(written))
        elif step:
            print(f"\nNothing to tune yet. Next step:\n  {step}")
        else:
            print("\nNothing written. Run again with --write to pin these models and save the settings.")
    return EXIT_NOTHING if step else EXIT_OK

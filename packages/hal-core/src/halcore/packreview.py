"""Pack install review: pinned fetch, what the pack can do, a typed `y` (REQ-SEC-84, REQ-SEC-85).

`hal ext add` calls `install` for every catalogue entry. Nothing of the pack runs, and nothing is
moved into the extensions folder, until the person has read the review and typed `y`.
Design decisions:

- Only the 40-hex commit pinned in `extensions.toml` is installed. A mirror (`HAL_GITHUB`) that
  serves another commit fails the checkout, so the pin cannot be bypassed by pointing at a fork.
  An entry without a pin is refused: an unpinned branch can change after it was reviewed.
- The pack is staged in a temporary folder, its manifest is parsed (never imported), and the
  review is built from the parsed manifest and the active policy. A manifest that does not load
  stops the install.
- Reviews are recorded per pack in `state_dir()/pack-reviews.json` (tag, commit and the reviewed
  facts). An update is compared with the recorded facts and shown as a diff when anything
  differs. `packs.json` itself belongs to the pack environments of 06-core-cli.
- Confirmation is typed on a terminal; there is no `--yes` (REQ-SEC-27). Install commands run
  through `halhub.install.run_steps`, which has a timeout and a checked exit code per step.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from halhub import install as hub_install
from halhub.manifest import ManifestError, ModuleSpec, load_manifest
from halhub.policy import Verdict, load_policy
from halkit import files, paths, proc
from halkit.termsafe import escape
from halkit.timeutil import iso_z

log = logging.getLogger(__name__)

COMMIT_RE = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT_S = 300
YES_ANSWERS = frozenset({"y", "yes"})
REVIEWS_FILE = "pack-reviews.json"


class PackRefused(RuntimeError):
    """The pack cannot be installed; the message says why and what to do."""


def reviews_path() -> Path:
    """`state_dir()/pack-reviews.json`."""
    return paths.state_dir() / REVIEWS_FILE


def facts(spec: ModuleSpec) -> dict[str, object]:
    """What REQ-SEC-84 shows about `spec`, as plain data that can be stored and compared."""
    effective = load_policy()
    tools: dict[str, object] = {}
    for tool in spec.tools:
        verdict = effective.tool(tool.meta(spec)).verdict
        tools[tool.name] = {
            "tier": tool.tier,
            "network": tool.network,
            "untrusted_output": tool.untrusted_output,
            "runs_code": tool.runs_code,
            "reversible": tool.reversible,
            "reviewable": tool.reviewable,
            "verdict": verdict.value,
            "params": {p.name: [p.format, p.access, p.content] for p in tool.params},
        }
    return {
        "version": spec.version,
        "tools": tools,
        "calls": list(spec.calls),
        "inprocess": spec.inprocess,
        "env": list(spec.env),
        "secrets": sorted(s.name for s in spec.secrets),
        "domains": sorted(spec.domains),
        "units": sorted(_lines(unit.file, "ExecStart=") for unit in spec.units),
        "desktop": sorted(_lines(path, "Exec=") for path in spec.desktop),
        "install": [" ".join(step.run) for step in spec.install],
    }


def _lines(path: Path, prefix: str) -> str:
    """The `prefix` lines of a unit or desktop file, joined; a file that cannot be read says so."""
    try:
        lines = [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.startswith(prefix)]
    except OSError as exc:
        return f"{path.name}: unreadable ({exc.strerror})"
    return f"{path.name}: " + " | ".join(lines)


def review_lines(data: dict[str, object]) -> list[str]:
    """The review text for `facts`; every tool that would run with no prompt is marked."""
    out = [f"version {data['version']}", "tools:"]
    for name, raw in data["tools"].items():  # type: ignore[union-attr]
        flags = [k for k in ("network", "untrusted_output", "runs_code", "reversible", "reviewable") if raw[k]]
        mark = "  NO PROMPT" if raw["verdict"] == Verdict.ALLOW.value else ""
        out.append(f"  {name}: tier {raw['tier']}, {', '.join(flags) or 'no flags'}, verdict {raw['verdict']}{mark}")
        for param, (fmt, access, content) in raw["params"].items():
            out.append(f"      {param}: format {fmt}, access {access}{', content' if content else ''}")
    for key in ("calls", "env", "secrets", "domains"):
        out.append(f"{key}: {', '.join(data[key]) or 'none'}")  # type: ignore[arg-type]
    out.append(f"inprocess: {'yes' if data['inprocess'] else 'no'}")
    for key in ("units", "desktop", "install"):
        out.append(f"{key}:")
        out.extend(f"  {line}" for line in data[key])  # type: ignore[union-attr]
        if not data[key]:
            out.append("  none")
    return [escape(line) for line in out]


def changes(old: dict[str, object], new: dict[str, object]) -> list[str]:
    """One line per fact that differs between two reviews (REQ-SEC-85); empty when nothing changed."""
    out: list[str] = []
    old_tools, new_tools = old["tools"], new["tools"]
    for name in sorted(set(old_tools) | set(new_tools)):  # type: ignore[arg-type]
        if name not in old_tools:  # type: ignore[operator]
            out.append(f"+ new tool {name}")
        elif name not in new_tools:  # type: ignore[operator]
            out.append(f"- tool {name} removed")
        elif old_tools[name] != new_tools[name]:  # type: ignore[index]
            out.append(f"~ tool {name}: {old_tools[name]} -> {new_tools[name]}")  # type: ignore[index]
    for key in ("calls", "inprocess", "env", "secrets", "domains", "units", "desktop", "install"):
        if old.get(key) != new[key]:
            out.append(f"~ {key}: {old.get(key)} -> {new[key]}")
    return [escape(line) for line in out]


def stage(repo: str, commit: str, parent: Path) -> Path:
    """Clones `repo` into a new folder under `parent` at exactly `commit`; refuses a mirror that differs."""
    if not COMMIT_RE.fullmatch(commit):
        raise PackRefused("the catalogue entry has no 40-hex `commit` pin; add one before installing")
    dest = parent / "pack"
    proc.run(["git", "clone", "--quiet", "--no-checkout", repo, str(dest)], timeout=GIT_TIMEOUT_S)
    try:
        proc.run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit], timeout=GIT_TIMEOUT_S)
        head = proc.run(["git", "-C", str(dest), "rev-parse", "HEAD"], timeout=GIT_TIMEOUT_S).stdout.decode().strip()
    except Exception as exc:
        raise PackRefused(f"{repo} does not have the pinned commit {commit}") from exc
    if head != commit:
        raise PackRefused(f"{repo} is at {head}, not the pinned {commit}")
    return dest


def typed_yes(prompt: str) -> bool:
    """True when a person typed `y` on a terminal; a pipe never counts."""
    if not sys.stdin.isatty():
        return False
    print(prompt, end="", flush=True)
    return sys.stdin.readline().strip().lower() in YES_ANSWERS


def _reviews() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(reviews_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def install(
    name: str,
    entry: dict[str, object],
    base: Path,
    *,
    confirm: Callable[[str], bool] = typed_yes,
    announce: Callable[[str], None] = print,
) -> bool:
    """Stages, reviews and installs one catalogue entry under `base`; False when the person declines.

    Raises `PackRefused` for a missing pin, a mirror that differs, or a manifest that does not
    load. An installed pack with the same commit is left alone.
    """
    repo, commit = str(entry["repo"]), str(entry.get("commit", ""))
    recorded = _reviews().get(name)
    if recorded and recorded.get("commit") == commit and (base / Path(repo).stem).is_dir():
        announce(f"  {name}: already installed at {commit[:12]}")
        return True
    with tempfile.TemporaryDirectory(prefix="hal-pack-") as scratch:
        staged = stage(repo, commit, Path(scratch))
        try:
            spec = load_manifest(staged / "hal-module.toml", source="path")
        except (ManifestError, OSError) as exc:
            raise PackRefused(f"{name}: the manifest does not load: {exc}") from exc
        now = facts(spec)
        diff = changes(recorded["facts"], now) if recorded else []  # type: ignore[arg-type]
        announce(f"{name} {escape(spec.version)} at {commit}")
        for line in diff if recorded else review_lines(now):
            announce(f"  {line}")
        if recorded and not diff:
            announce("  no change to tools, flags, units or install commands")
        elif not confirm("Type y and Enter to install this pack: "):
            announce("nothing installed")
            return False
        dest = base / Path(repo).stem
        if dest.exists():
            shutil.rmtree(dest)
        base.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, dest)
    final = load_manifest(dest / "hal-module.toml", source="path")
    hub_install.run_steps(
        final, confirm=lambda step: confirm(f"Run install command {step.id}: {' '.join(step.run)}\nType y and Enter: ")
    )
    record = _reviews()
    record[name] = {"repo": repo, "commit": commit, "facts": now, "installed": iso_z()}
    files.write_atomic(reviews_path(), json.dumps(record, indent=2, sort_keys=True) + "\n")
    return True

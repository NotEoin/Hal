"""Agent Skills discovery: `SKILL.md` files listed in the system prompt, loaded on demand.

Only the name and description go into the prompt (progressive loading); the model reads the
file with `read` when a skill fits. `allowed-tools` is ignored: the harness, not a skill file,
decides what may run. Invalid skills are skipped and reported, never fatal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("halagent.skills")

MAX_SKILLS = 12
MAX_LIST_CHARS = 3_000
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1_024
MAX_COMPAT_CHARS = 500
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SCALAR_KEYS = {"name", "description", "license", "compatibility", "allowed-tools"}
PROJECT_DIRS = (".agents/skills", ".claude/skills")


@dataclass(frozen=True)
class Skill:
    """One discovered skill."""

    name: str
    description: str
    path: Path


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parses the block between the first two `---` lines: scalars and one `metadata:` mapping."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("no frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("frontmatter is not closed") from None
    out: dict[str, object] = {}
    meta: dict[str, str] | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if meta is None or ":" not in line:
                raise ValueError(f"unexpected indented line {line.strip()!r}")
            k, _, v = line.partition(":")
            meta[k.strip()] = _scalar(v)
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"expected 'key: value', got {line!r}")
        key = key.strip()
        if key == "metadata" and not value.strip():
            meta = out["metadata"] = {}
        else:
            meta = None
            out[key] = _scalar(value)
    return out


def _load(path: Path) -> Skill:
    fm = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    name, desc = fm.get("name"), fm.get("description")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or len(name) > MAX_NAME_CHARS:
        raise ValueError("name must be 1 to 64 lowercase letters, digits and hyphens")
    if name != path.parent.name:
        raise ValueError(f"name {name!r} must equal the directory name {path.parent.name!r}")
    if not isinstance(desc, str) or not desc or len(desc) > MAX_DESCRIPTION_CHARS:
        raise ValueError("description is required, at most 1024 characters")
    compat = fm.get("compatibility", "")
    if isinstance(compat, str) and len(compat) > MAX_COMPAT_CHARS:
        raise ValueError("compatibility is over 500 characters")
    return Skill(name, desc, path)


def discover_skills(root: Path, user_dirs: tuple[Path, ...] = ()) -> tuple[list[Skill], list[str]]:
    """Returns (skills, problems); the first directory wins on a name clash."""
    skills: dict[str, Skill] = {}
    problems: list[str] = []
    dirs = [root / d for d in PROJECT_DIRS] + list(user_dirs)
    for base in dirs:
        for path in sorted(base.glob("*/SKILL.md")) if base.is_dir() else []:
            try:
                skill = _load(path)
            except (ValueError, OSError) as exc:
                problems.append(f"skipped skill {path}: {exc}")
                log.warning("skipped skill %s: %s", path, exc)
                continue
            skills.setdefault(skill.name, skill)
    return list(skills.values()), problems


def render_skills(skills: list[Skill], max_listed: int = MAX_SKILLS, max_chars: int = MAX_LIST_CHARS) -> str:
    """The "Skills" prompt section, cut to `max_listed` skills and `max_chars` characters; empty with none."""
    if not skills:
        return ""
    head = (
        "## Skills\n\nSkills are instructions for particular jobs. When one fits the task, "
        "read its SKILL.md with the read tool first, then follow it."
    )
    out = [head]
    size = len(head)
    for skill in skills[:max_listed]:
        line = f"- {skill.name}: {skill.description} ({skill.path})"
        if size + len(line) + 1 > max_chars:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)

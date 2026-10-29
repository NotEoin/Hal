"""Documentation gates: requirement ids in the design specs and the docs index.

REQ-SYS-19: every requirement id in `docs/design/*.md` is `REQ-<PREFIX>-<nn>`, unique across the
specs, uses the prefix of the spec that defines it, and names a test.
REQ-SYS-25: `docs/README.md` indexes every file under `docs/` in a `| Document | What it is |`
table, and each folder with its own README does the same for its files.

A requirement id has two or three digits: the specs go past 99 (for example `REQ-CLI-111`), so
the pattern in REQ-SYS-19 is read as "at least two digits".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
DESIGN = DOCS / "design"
ID_RE = re.compile(r"^REQ-([A-Z]+)-(\d{2,3})$")
ITEM_RE = re.compile(r"^- \*\*(REQ-[A-Z]+-\d+)\*\*")
TEST_RE = re.compile(r"\bTests?:")
# the prefix each spec owns, from the "Requirements index" in 00-overview.md
PREFIX_OF = {
    "00-overview.md": "SYS",
    "01-security.md": "SEC",
    "02-sandbox.md": "BOX",
    "03-agent.md": "AGT",
    "04-hub.md": "HUB",
    "05-gateway.md": "GW",
    "06-core-cli.md": "CLI",
    "07-remote.md": "RMT",
}
TABLE_HEADER = "| Document | What it is |"
ROW_LINK_RE = re.compile(r"^\|\s*\[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\)\s*\|")


def requirement_items(text: str) -> list[tuple[str, int, str]]:
    """Returns `(id, line, body)` for each list item that starts with a bold requirement id.

    The body runs to the next blank line or list item, so it holds the whole requirement.
    """
    lines = text.split("\n")
    items = []
    index = 0
    while index < len(lines):
        match = ITEM_RE.match(lines[index])
        if not match:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() and not lines[end].startswith("- "):
            end += 1
        items.append((match.group(1), index + 1, "\n".join(lines[index:end])))
        index = end
    return items


def all_items() -> list[tuple[str, str, int, str]]:
    found = []
    for name in sorted(PREFIX_OF):
        for ident, line, body in requirement_items((DESIGN / name).read_text(encoding="utf-8")):
            found.append((name, ident, line, body))
    return found


def indexed_paths(readme: Path) -> list[str]:
    """Link targets in the first column of the `Document | What it is` table of `readme`."""
    paths = []
    in_table = False
    for line in readme.read_text(encoding="utf-8").split("\n"):
        if line.strip() == TABLE_HEADER:
            in_table = True
        elif in_table:
            row = ROW_LINK_RE.match(line)
            if row:
                paths.append(row.group(1))
            elif not line.startswith("|"):
                break
    return paths


def folders_with_an_index() -> list[Path]:
    return sorted(p.parent for p in DOCS.rglob("README.md"))


def test_every_spec_is_known_to_the_prefix_table():
    on_disk = {p.name for p in DESIGN.glob("0*.md")}
    assert on_disk == set(PREFIX_OF)


def test_requirement_ids_have_the_documented_shape():
    bad = [(n, i) for n, i, _, _ in all_items() if not ID_RE.match(i)]
    assert bad == []


def test_requirement_ids_are_unique_across_the_specs():
    ids = [i for _, i, _, _ in all_items()]
    assert sorted({i for i in ids if ids.count(i) > 1}) == []


def test_each_spec_defines_only_its_own_prefix():
    wrong = [(n, i) for n, i, _, _ in all_items() if ID_RE.match(i).group(1) != PREFIX_OF[n]]
    assert wrong == []


def test_every_requirement_names_a_test_unless_withdrawn():
    missing = [
        f"{name}:{line} {ident}"
        for name, ident, line, body in all_items()
        if not TEST_RE.search(body) and "Withdrawn" not in body
    ]
    assert missing == []


def test_the_specs_define_a_plausible_number_of_requirements():
    assert len(all_items()) > 500  # a parser that matches nothing would otherwise pass every test


def test_requirement_items_reads_a_multi_line_item_up_to_the_next_item():
    text = "- **REQ-X-01** First line\n  continues. Test: unit (a).\n- **REQ-X-02** Second.\n\nprose\n"
    items = requirement_items(text)
    assert [(i, line) for i, line, _ in items] == [("REQ-X-01", 1), ("REQ-X-02", 3)]
    assert "Test: unit (a)" in items[0][2] and "prose" not in items[1][2]


def test_requirement_items_ignores_ids_that_are_not_list_items():
    assert requirement_items("See REQ-SYS-01 for details.\n| REQ-SYS-02 | x |\n") == []


def test_docs_readme_exists_with_the_index_table():
    assert TABLE_HEADER in (DOCS / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "folder", folders_with_an_index(), ids=lambda p: p.relative_to(DOCS).as_posix() or "."
)
def test_folder_index_lists_every_file_in_the_folder_and_nothing_else(folder):
    readme = folder / "README.md"
    # docs/README.md covers the whole tree; a folder's own README covers that folder
    scope = DOCS.rglob("*") if folder == DOCS else folder.glob("*")
    expected = {p.relative_to(folder).as_posix() for p in scope if p.is_file() and p != readme}
    listed = indexed_paths(readme)
    assert len(listed) == len(set(listed)), "a file is listed twice"
    assert set(listed) == expected
    assert all((folder / path).is_file() for path in listed)


def test_docs_index_links_the_overview():
    assert "design/00-overview.md" in indexed_paths(DOCS / "README.md")


def test_claude_md_points_to_agents_md_and_holds_no_rules_of_its_own():
    claude = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "[AGENTS.md](AGENTS.md)" in claude and "@AGENTS.md" in claude
    assert len(claude.splitlines()) < 10


def test_agents_md_commands_are_the_ones_ci_runs():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for command in (
        "uv run ruff check .",
        "uv run ruff format --check packages tests",
        "scripts/check-ascii.sh",
    ):
        assert command in agents and command in workflow


def test_security_policy_and_contributing_guide_exist_and_link_the_threat_model():
    security = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "docs/design/01-security.md" in security
    assert "SECURITY.md" in contributing and "AGENTS.md" in contributing


def test_relative_links_in_the_root_documents_resolve():
    link = re.compile(r"\]\((?!https?:|#|mailto:)([^)#]+)")
    for name in ("AGENTS.md", "CLAUDE.md", "SECURITY.md", "CONTRIBUTING.md"):
        for target in link.findall((REPO / name).read_text(encoding="utf-8")):
            assert (REPO / target).exists(), f"{name} links to missing {target}"

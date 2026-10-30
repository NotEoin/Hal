"""Tests for `halagent.instructions`, `halagent.skills` and their use in the system prompt."""

import pytest

from halagent.instructions import load_instructions
from halagent.loop import Agent
from halagent.settings import AgentSettings
from halagent.skills import discover_skills, parse_frontmatter, render_skills
from halagent.tools import Toolset


def test_agents_md_files_load_root_first_and_later_wins_note_is_added(tmp_path):
    (tmp_path / "AGENTS.md").write_text("root rules")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "AGENTS.md").write_text("sub rules")
    ins = load_instructions(tmp_path, tmp_path / "sub")
    assert ins.files == ("AGENTS.md", "sub/AGENTS.md")
    assert ins.text.index("root rules") < ins.text.index("sub rules") and "later one wins" in ins.text


def test_claude_md_is_used_only_when_agents_md_is_absent(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude only")
    assert load_instructions(tmp_path, tmp_path).files == ("CLAUDE.md",)
    (tmp_path / "AGENTS.md").write_text("agents")
    assert load_instructions(tmp_path, tmp_path).files == ("AGENTS.md",)


def test_claude_md_that_only_imports_agents_md_is_skipped_as_a_duplicate(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n")
    assert load_instructions(tmp_path, tmp_path).files == ("AGENTS.md",)


def test_import_line_is_replaced_inside_the_workspace_only(tmp_path):
    (tmp_path / "AGENTS.md").write_text("see:\n@docs.md\n@../outside.md")
    (tmp_path / "docs.md").write_text("DOCS TEXT")
    (tmp_path.parent / "outside.md").write_text("OUTSIDE")
    text = load_instructions(tmp_path, tmp_path).text
    assert "DOCS TEXT" in text and "OUTSIDE" not in text and "@../outside.md" in text


def test_total_cap_cuts_files_from_the_root_end_and_says_so(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a" * 500)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "AGENTS.md").write_text("b" * 100)
    ins = load_instructions(tmp_path, tmp_path / "sub", max_chars=300)
    assert ins.truncated and "[hal] instructions cut: AGENTS.md" in ins.text and "a" * 50 not in ins.text


def test_cwd_outside_the_workspace_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="outside the workspace"):
        load_instructions(tmp_path / "w", tmp_path)


def skill(root, name, desc="Does a thing.", where=".agents/skills", dirname=None):
    d = root / where / (dirname or name)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{desc}"\nmetadata:\n  author: x\n---\nBody\n'
    )


def test_frontmatter_parses_scalars_and_one_metadata_level():
    fm = parse_frontmatter("---\nname: a\ndescription: 'b: c'\nmetadata:\n  k: v\n---\nbody")
    assert fm == {"name": "a", "description": "b: c", "metadata": {"k": "v"}}


def test_skills_are_discovered_in_order_and_first_wins_on_a_clash(tmp_path):
    skill(tmp_path, "pdf-tools", "from agents")
    skill(tmp_path, "pdf-tools", "from claude", where=".claude/skills")
    skill(tmp_path, "other", where=".claude/skills")
    skills, problems = discover_skills(tmp_path)
    assert {s.name: s.description for s in skills} == {"pdf-tools": "from agents", "other": "Does a thing."}
    assert problems == []


def test_skill_with_a_name_that_differs_from_its_directory_is_skipped_with_a_problem(tmp_path):
    skill(tmp_path, "good-name", dirname="other-dir")
    skills, problems = discover_skills(tmp_path)
    assert skills == [] and "must equal the directory name" in problems[0]


def test_skill_listing_is_capped_at_twelve_entries(tmp_path):
    for i in range(15):
        skill(tmp_path, f"s{i}")
    text = render_skills(discover_skills(tmp_path)[0])
    assert text.count("\n- ") == 12 and "read its SKILL.md with the read tool" in text


def test_agent_puts_instructions_in_the_system_prompt(server, tmp_path):
    agent = Agent(
        AgentSettings(endpoint=server.url),
        Toolset(tmp_path),
        system_extra="## Project instructions\n\nbe terse",
    )
    assert agent.messages[0]["content"].endswith("be terse")


def test_cli_sends_agents_md_and_skills_to_the_model(server, tmp_path, monkeypatch):
    from halagent import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "AGENTS.md").write_text("use tabs")
    skill(tmp_path, "pdf-tools")
    server.script("ok")
    assert cli.main(["-p", "x", "-C", str(tmp_path), "--endpoint", server.url, "--no-session"]) == 0
    system = server.requests[0]["body"]["messages"][0]["content"]
    assert "use tabs" in system and "- pdf-tools: Does a thing." in system

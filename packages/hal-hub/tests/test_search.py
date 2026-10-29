"""Tests for `halhub.search` (REQ-HUB-34): BM25 ranking on synthetic tools and a pack-free synonym table."""

from __future__ import annotations

import tomllib
from pathlib import Path

from halhub import search
from halhub.manifest import ToolSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# RESOLUTIONS C7 (the hal-tools packs) and the modules in 04-hub "Migration", which took the v1
# synonyms into their manifest keywords
HAL_TOOLS_PACKS = ("organiser", "life", "comms", "online", "desktop", "system", "work", "jobs")
HAL_TOOLS_MODULES = (
    "rgb", "media", "desktop", "screen", "web", "memory", "calendar", "tasks", "clock", "system",
    "files", "mail", "jobs", "phone", "network", "power", "packages", "devices", "dev", "habits",
    "ledger", "contacts", "briefing", "routines", "models", "lang",
)  # fmt: skip


def tool(name: str, description: str = "", *, module: str | None = None, keywords: tuple = ()) -> ToolSpec:
    module = module or name.split("_")[0]
    return ToolSpec(
        name=name,
        module=module,
        pack=module,
        function=name,
        description=description,
        tier="read",
        tier_inferred=False,
        keywords=keywords,
    )


def names(found: list[ToolSpec]) -> list[str]:
    return [t.name for t in found]


def test_bm25_keeps_the_okapi_defaults_and_the_quarter_floor():
    assert (search.K1, search.B, search.SCORE_FLOOR) == (1.2, 0.75, 0.25)


def test_pack_keywords_rank_pack_tool_first():
    tools = [
        tool("rgb_set", "Set strip colour"),
        tool("media_play", "Play something"),
        tool("clock_alarm", "Ring alarm"),
    ]
    assert search.rank("lights red", tools) == []
    found = search.rank("lights red", tools, module_keywords={"rgb": ("lights", "light")})
    assert names(found) == ["rgb_set"]


def test_tool_keywords_count_like_description_words():
    tools = [tool("media_play", "Play audio", keywords=("music", "song")), tool("web_read", "Read a page")]
    assert names(search.rank("play a song", tools))[0] == "media_play"


def test_generic_synonym_reaches_the_tool_word():
    tools = [tool("calendar_add", "Add an event"), tool("tasks_add", "Add a task")]
    assert names(search.rank("book meeting", tools)) == ["calendar_add"]
    assert names(search.rank("new chore", tools)) == ["tasks_add"]


def test_synonyms_name_no_pack():
    fixture_modules = {
        tomllib.loads(m.read_text())["module"]["name"] for m in FIXTURES.glob("*/hal-module.toml")
    }
    forbidden = fixture_modules | set(HAL_TOOLS_PACKS) | set(HAL_TOOLS_MODULES)
    words = {w for value in search.SYNONYMS.values() for w in value.split()}
    assert fixture_modules and not words & forbidden
    assert not set(search.SYNONYMS) & forbidden


def test_plural_query_word_matches_singular():
    tools = [tool("tasks_list", "Show every task"), tool("web_read", "Read a page")]
    assert names(search.rank("tasks", tools))[0] == "tasks_list"
    assert names(search.rank("chores", tools)) == ["tasks_list"]


def test_matches_under_the_floor_are_dropped():
    tools = [
        tool("calendar_add", "Add an event to the calendar calendar calendar"),
        tool("notes_add", "Add a note about the day"),
    ]
    assert names(search.rank("calendar add", tools)) == ["calendar_add"]


def test_ties_sort_by_tool_name():
    tools = [tool("zeta_ping", "ping"), tool("alpha_ping", "ping")]
    assert names(search.rank("ping", tools)) == ["alpha_ping", "zeta_ping"]


def test_limit_and_empty_input():
    tools = [tool(f"t{i}_ping", "ping") for i in range(5)]
    assert len(search.rank("ping", tools, limit=2)) == 2
    assert search.rank("ping", []) == []
    assert search.rank("", tools) == []

"""Tests for the `repo_map` tool."""

from halagent.tools import Toolset
from halagent.tools.repomap import build_map, python_symbols, regex_symbols


def make_tree(root):
    (root / "pkg").mkdir()
    (root / "pkg" / "billing.py").write_text(
        "class Invoice:\n    def total(self):\n        return 1\n    def _hidden(self):\n        pass\n\n"
        "def charge(x):\n    return x\n"
    )
    (root / "pkg" / "users.py").write_text("def login(name):\n    return name\n")
    (root / "web.ts").write_text("export function render(a: number) {\n}\nexport class Page {}\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("function junk() {}\n")
    (root / "broken.py").write_text("def (:\n")


def test_python_symbols_list_classes_with_public_methods_and_functions():
    src = "class A:\n    def go(self): ...\n    def _x(self): ...\n\ndef f(): ...\n"
    assert python_symbols(src) == ["class A: go", "def f"]


def test_regex_fallback_finds_definitions_in_other_languages():
    assert regex_symbols("export function render(a) {\n}\npub fn run() {}\nstruct Foo {\n") == [
        "export function render",
        "pub fn run",
        "struct Foo",
    ]


def test_map_ranks_files_matching_the_query_first(tmp_path):
    make_tree(tmp_path)
    out = build_map(tmp_path, "fix the invoice total").splitlines()
    assert out[0] == "pkg/billing.py" and "  class Invoice: total" in out
    assert "node_modules/junk.js" not in "\n".join(out)


def test_map_is_deterministic_and_capped_to_the_token_budget(tmp_path):
    make_tree(tmp_path)
    for k in range(60):
        (tmp_path / f"mod{k}.py").write_text("def f():\n    pass\n" * 1)
    first = build_map(tmp_path, "", max_tokens=100)
    assert first == build_map(tmp_path, "", max_tokens=100)
    assert len(first) < 100 * 4 + 200


def test_repo_map_tool_is_offered_and_runs(tmp_path):
    make_tree(tmp_path)
    ts = Toolset(tmp_path)
    assert "repo_map" in ts.tools
    assert "pkg/users.py" in ts.tools["repo_map"].fn({"query": "login"})

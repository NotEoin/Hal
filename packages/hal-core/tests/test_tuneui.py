"""Tests for the screen of the interactive `hal tune`: layout, menus, commands and keys, with no terminal.

The menus and commands run through a scripted prompter; the real `PtPrompter` is driven by prompt_toolkit's
pipe input and a dummy output. Hardware and models are fakes.
"""

from __future__ import annotations

import io
import json
import tomllib

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from test_tune import CARD_24GB, CPU_ONLY, GIB, SHOW, TAGS, arch_doc

from halcore import tune, tuneui
from halcore.tui.theme import ASCII, UNICODE, load_look, make_console
from halkit import hardware
from halkit.testing import TempHalHome
from halkit.testing.ollama import FakeOllama

QWEN_TAG = {"name": "qwen3:8b", "size": 5 * GIB, "digest": "f" * 64, "details": {"family": "qwen3", "parameter_size": "8B"}}


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


@pytest.fixture
def server():
    with FakeOllama([*TAGS, QWEN_TAG], {**SHOW, "qwen3:8b": arch_doc(36, 32, 8, 128, 40960)}) as s:
        yield s


class Script:
    """A prompter that answers from lists; a pick answer matches an item value or part of its label."""

    def __init__(self, asks=(), picks=()):
        self.asks, self.picks, self.seen = list(asks), list(picks), []

    def ask(self, text, choices=()):
        return self.asks.pop(0) if self.asks else None

    def pick(self, title, items, current="", **kwargs):
        self.seen.append((title, [label for label, _ in items]))
        want = self.picks.pop(0) if self.picks else None
        if want is None:
            return None
        if want in kwargs.get("keys", ""):
            return want
        return next(value for label, value in items if want == value or want in label)


def open_screen(home, server, hw=CARD_24GB, asks=(), picks=(), unicode=True, width=100):
    home.config.write_text(f'# my notes\n[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n')
    out = io.StringIO()
    look = load_look({"unicode": unicode, "theme": "mono"}, stream=io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    console = make_console(look, out, width=width)
    state = tuneui.open_state(hw=hardware.from_dict(hw))
    prompter = Script(asks, picks)
    return tuneui.TuneScreen(state, look, prompter, console), out, prompter


def test_the_main_screen_shows_hardware_sources_and_roles_within_80_columns(home, server):
    screen, out, _ = open_screen(home, server, width=80)
    screen.state.auto_tune()
    screen.console.print(tuneui.render_screen(screen.state, screen.look, "chat"))
    text = out.getvalue()
    assert "RTX 4090  24000 / 24564 MiB free" in text and "local  ollama" in text and "gemma:12b" in text
    assert "of" in text and "GiB VRAM" in text
    assert max(len(line) for line in text.splitlines()) <= 80


def test_a_machine_without_a_gpu_says_so_and_plans_on_the_cpu(home, server):
    screen, out, _ = open_screen(home, server, CPU_ONLY)
    screen.state.auto_tune("chat")
    screen.console.print(tuneui.render_screen(screen.state, screen.look, "chat"))
    text = out.getvalue()
    assert "GPU  none: models run on the CPU" in text and "in system RAM, CPU only" in text


def test_the_ascii_look_draws_only_ascii(home, server):
    screen, out, _ = open_screen(home, server, unicode=False)
    assert screen.look.glyphs is ASCII and UNICODE is not ASCII
    screen.state.auto_tune()
    screen.console.print(tuneui.render_screen(screen.state, screen.look, "chat"))
    assert out.getvalue().isascii()


def test_wsl2_and_detection_notes_are_shown(home, server):
    lines = tuneui.hardware_lines(hardware.from_dict({**CARD_24GB, "wsl2": True, "notes": ["nvidia-smi was slow"]}))
    assert any(line.startswith("WSL2") for line in lines) and "note: nvidia-smi was slow" in lines


def test_choosing_a_model_context_and_writing_goes_through_the_apply_path(home, server):
    screen, out, prompter = open_screen(
        home, server,
        asks=["model", "context", "effort", "write", "quit"],
        picks=["local\nqwen3:8b", "32768", "medium", "yes"],
    )
    assert screen.run_commands() == 0
    chat = tomllib.loads(home.config.read_text())["models"]["roles"]["chat"]
    assert (chat["model"], chat["context"], chat["effort"], chat["auto"]) == ("qwen3:8b", 32768, "medium", False)
    assert home.config.read_text().startswith("# my notes")
    titles = [t for t, _ in prompter.seen]
    assert titles == ["chat model", "chat context", "chat effort", "Write these changes?"]
    context_items = prompter.seen[1][1]
    assert any("GB" in label and "fits" in label for label in context_items)  # every preset shows its fit
    assert "wrote" in out.getvalue()


def test_effort_is_not_offered_for_a_model_without_the_capability(home, server):
    screen, out, prompter = open_screen(home, server, asks=["model", "effort", "quit"], picks=["gemma:12b"])
    screen.run_commands()
    assert [t for t, _ in prompter.seen] == ["chat model"]  # no effort picker opened
    assert "no thinking control" in out.getvalue()


def test_quitting_without_writing_leaves_the_config_alone(home, server):
    screen, out, _ = open_screen(home, server, asks=["auto all", "write", "q"], picks=["no"])
    before = home.config.read_text()
    assert screen.run_commands() == 0
    assert home.config.read_text() == before and "Not written." in out.getvalue()
    assert "unchanged" in out.getvalue()


def test_end_of_input_quits_without_writing(home, server):
    screen, _, _ = open_screen(home, server, asks=["auto"])
    before = home.config.read_text()
    assert screen.run_commands() == 0 and home.config.read_text() == before


def test_a_context_typed_in_k_is_parsed_and_a_bad_one_is_reported_not_raised(home, server):
    screen, out, _ = open_screen(home, server, asks=["auto chat", "context chat 48k", "context chat banana", "quit"])
    screen.run_commands()
    assert screen.state.drafts["chat"].context == 48 * 1024
    assert "not a number of tokens" in out.getvalue()


def test_a_failed_write_is_reported_and_leaves_the_config(home, server, monkeypatch):
    screen, out, _ = open_screen(home, server, asks=["auto chat", "write", "quit"], picks=["yes"])
    before = home.config.read_text()
    monkeypatch.setattr(tuneui.tunestate.modelplan.gwmodels, "record_ollama", lambda *a, **k: (_ for _ in ()).throw(
        tuneui.tunestate.modelplan.ManifestError("disk full")))
    screen.run_commands()
    assert "nothing was written" in out.getvalue() and home.config.read_text() == before


def test_sources_can_be_added_and_removed_from_the_screen(home, server, tmp_path):
    folder = tmp_path / "weights"
    folder.mkdir()
    screen, out, _ = open_screen(home, server, asks=["source add", str(folder), "source remove", "quit"], picks=["gguf", "gguf"])
    screen.run_commands()
    assert "added gguf: 0 models" in out.getvalue() and "removed gguf" in out.getvalue()
    assert list(screen.state.sources) == ["local"]


def test_fuzzy_filter_matches_characters_in_order():
    assert tuneui.fuzzy("g12", "local/gemma:12b") and not tuneui.fuzzy("12g", "local/gemma:12b")
    assert tuneui.parse_tokens("32k") == 32768 and tuneui.parse_tokens("8192") == 8192


def run_pt(keys, call):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        return call(tuneui.PtPrompter(load_look({}), input=pipe, output=DummyOutput()))


ITEMS = [("local/gemma:12b", "a"), ("local/tiny:1b", "b"), ("weights/coder.gguf", "c")]


def test_the_picker_moves_with_the_arrow_keys_and_enter_chooses():
    assert run_pt("\x1b[B\x1b[B\r", lambda p: p.pick("model", ITEMS)) == "c"
    assert run_pt("\x1b[B\x1b[A\r", lambda p: p.pick("model", ITEMS)) == "a"


def test_the_picker_filters_by_typing():
    assert run_pt("tin\r", lambda p: p.pick("model", ITEMS)) == "b"
    assert run_pt("zzz\r", lambda p: p.pick("model", ITEMS)) is None  # no match: Enter chooses nothing


def test_ctrl_c_cancels_the_picker_and_the_command_line():
    assert run_pt("\x03", lambda p: p.pick("model", ITEMS)) is None
    assert run_pt("\x03", lambda p: p.ask("tune> ")) is None
    assert run_pt("model\r", lambda p: p.ask("tune> ", ["model"])) == "model"


def test_a_bare_tune_on_a_terminal_opens_the_screen_and_otherwise_prints_the_plan(home, server, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(tuneui, "run_interactive", lambda **kw: opened.append(kw) or 0)
    monkeypatch.setattr(tune, "is_interactive", lambda: True)
    assert tune.main([]) == 0 and len(opened) == 1
    assert tune.main(["--auto"]) in (0, 3) and len(opened) == 1  # a flag keeps the old behaviour
    monkeypatch.setattr(tune, "is_interactive", lambda: False)
    tune.main([])
    assert len(opened) == 1 and "role" in capsys.readouterr().out


def test_json_output_is_unchanged_by_the_screen(home, server, tmp_path, capsys):
    home.config.write_text(f'[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n')
    hw = tmp_path / "hw.json"
    hw.write_text(json.dumps(CARD_24GB))
    assert tune.main(["--auto", "--json", "--hardware", str(hw)]) == 0
    assert json.loads(capsys.readouterr().out)["hardware"]["gpus"][0]["name"] == "RTX 4090"


def test_a_gpu_held_by_another_program_shows_a_warning_line():
    # regression: sizing used free VRAM, so a busy card silently gave tiny contexts
    busy = hardware.from_dict({**CARD_24GB, "gpus": [{**CARD_24GB["gpus"][0], "free_mib": 2000}]})
    lines = tuneui.hardware_lines(busy)
    assert sum("another program is using" in line for line in lines) == 1
    assert not any("another program" in line for line in tuneui.hardware_lines(hardware.from_dict(CARD_24GB)))


def test_the_menus_choose_a_model_and_context_and_save_without_typing(home, server):
    screen, out, prompter = open_screen(
        home, server,
        picks=["role", "chat", "model", "local\nqwen3:8b", "context", "32768", "back", None, "save", "yes"],
    )
    assert screen.run() == 0
    chat = tomllib.loads(home.config.read_text())["models"]["roles"]["chat"]
    assert (chat["model"], chat["context"], chat["auto"]) == ("qwen3:8b", 32768, False)
    titles = [t for t, _ in prompter.seen]
    assert titles[:2] == ["What next?", "Adjust which role?"] and "Write these changes?" in titles
    assert "wrote" in out.getvalue()


def test_the_role_menu_offers_every_setting_and_the_context_menu_shows_each_fit(home, server):
    screen, _, prompter = open_screen(home, server, picks=["role", "chat", "context", None, "back", None, "quit"])
    screen.state.auto_tune("chat")
    screen.run()
    role_items = prompter.seen[2][1]
    assert role_items == ["Model", "Context", "KV cache", "Effort", "Temperature", "Auto-tune this role", "Back"]
    context_items = prompter.seen[3][1]
    assert [i.split()[0] for i in context_items[1:6]] == ["8K", "16K", "32K", "64K", "128K"]
    assert context_items[-1] == "Custom..." and all("GB" in i for i in context_items[1:6])


def test_esc_on_the_main_menu_asks_before_dropping_changes(home, server):
    screen, out, _ = open_screen(home, server, picks=[None, "no", None, "yes"])
    screen.state.auto_tune("chat")
    assert screen.run() == 0 and "unchanged" in out.getvalue()


def test_esc_on_the_main_menu_quits_at_once_when_nothing_changed(home, server):
    screen, _, prompter = open_screen(home, server, picks=[None])
    assert screen.run() == 0 and len(prompter.seen) == 1


def test_a_colon_opens_the_typed_commands_and_menu_returns_to_the_menus(home, server):
    screen, _, prompter = open_screen(home, server, asks=["menu"], picks=[":", "quit"])
    assert screen.run() == 0 and [t for t, _ in prompter.seen] == ["What next?", "What next?"]
    screen, _, _ = open_screen(home, server, asks=["quit"], picks=[":"])
    assert screen.run() == 0


def test_a_menu_error_is_shown_not_raised(home, server):
    screen, out, _ = open_screen(home, server, picks=["role", "writer", "context", "back", None, "quit"])
    screen.state.drafts.pop("writer", None)
    screen.run()
    assert "has no model yet" in out.getvalue()


def test_the_summary_fits_80_columns_in_ascii_and_lists_every_role(home, server):
    screen, out, _ = open_screen(home, server, unicode=False, width=80)
    screen.state.auto_tune()
    screen.console.print(tuneui.render_summary(screen.state, screen.look))
    text = out.getvalue()
    assert text.isascii() and max(len(line) for line in text.splitlines()) <= 80
    assert all(role in text for role in ("chat", "fast", "code", "writer", "vision", "embed"))
    assert len(text.splitlines()) + len(tuneui.MAIN_ITEMS) + 2 <= 24


def test_fit_words_say_fits_tight_or_too_big():
    fit = tuneui.tunestate.Fit
    assert tuneui.fit_word(fit("gpu", used_mib=5000, budget_mib=20000)) == "fits"
    assert tuneui.fit_word(fit("gpu", used_mib=19000, budget_mib=20000)) == "tight"
    assert tuneui.fit_word(fit("hybrid")) == "tight, uses RAM"
    assert tuneui.fit_word(fit("does-not-fit")) == "too big"


def test_number_keys_choose_a_row_and_q_or_esc_go_back():
    assert run_pt("2", lambda p: p.pick("menu", ITEMS, filterable=False)) == "b"
    assert run_pt("q", lambda p: p.pick("menu", ITEMS, filterable=False)) is None
    assert run_pt("\x1b", lambda p: p.pick("menu", ITEMS, filterable=False)) is None
    assert run_pt("\x1b[B\r", lambda p: p.pick("menu", ITEMS, "a", filterable=False)) == "b"


def test_a_menu_returns_an_extra_key_only_when_it_is_asked_for():
    assert run_pt(":", lambda p: p.pick("menu", ITEMS, filterable=False, keys=":")) == ":"
    assert run_pt(":\x1b", lambda p: p.pick("menu", ITEMS, filterable=False)) is None


def test_the_hint_footer_names_the_keys_in_ascii_mode():
    view = tuneui.ListView("t", ITEMS, "", load_look({"unicode": False}), {"accent": "", "muted": ""}, filterable=False, keys=":")
    assert view.hint().startswith("up/down move | enter select | 1-9 jump | esc back") and view.hint().isascii()


def test_clicking_a_row_chooses_it_and_other_mouse_events_are_ignored():
    view = tuneui.ListView("t", ITEMS, "", load_look({}), {"accent": "", "muted": ""}, filterable=False)
    handlers = [frag[2] for frag in view.fragments() if len(frag) == 3]
    assert len(handlers) == 3

    def mouse(kind):
        return MouseEvent(Point(0, 0), kind, MouseButton.LEFT, frozenset())

    assert handlers[2](mouse(MouseEventType.MOUSE_DOWN)) is NotImplemented and view.result is None
    handlers[2](mouse(MouseEventType.MOUSE_UP))
    assert view.result == "c" and view.finished


def test_quick_setup_shows_the_four_presets_and_previews_the_result_without_writing(home, server):
    screen, out, prompter = open_screen(home, server, picks=["preset", "gpu-12", None, "yes"])
    before = home.config.read_text()
    assert screen.run() == 0
    labels = prompter.seen[1][1]
    assert [lb.split("  ")[0].strip() for lb in labels] == ["This machine (auto)", "12 GB GPU", "24 GB GPU", "CPU only"]
    text = out.getvalue()
    assert "config.toml (new)" in text and "preview" in text and "Planning for: 12 GB GPU" in text
    assert home.config.read_text() == before


def test_the_preset_menu_marks_nothing_as_changed_for_a_cancelled_choice(home, server):
    screen, _, _ = open_screen(home, server, picks=["preset", None, "quit"])
    screen.run()
    assert not screen.state.has_changes() and screen.state.preset is None

"""Pilot tests for the full-screen `hal tune`: presets, sliders, segments, sources, undo, save and quit.

Hardware and models are fakes (`FakeOllama`, `TempHalHome`); nothing touches a GPU or the real config.
"""

from __future__ import annotations

import asyncio
import io
import tomllib

import pytest
from test_tune import CARD_24GB, CPU_ONLY, SHOW, TAGS, arch_doc
from test_tuneui import QWEN_TAG
from textual.widgets import Button, OptionList

from halcore import tuneapp, tunestate
from halcore.tui import screens
from halcore.tui.theme import load_look
from halcore.tui.widgets import FitGauge, Segmented, Slider
from halkit import hardware
from halkit.testing import TempHalHome
from halkit.testing.ollama import FakeOllama

SIZE = (110, 40)
LOOK = load_look({"unicode": False, "theme": "dark", "animations": False}, stream=io.StringIO(), environ={})


@pytest.fixture
def home():
    with TempHalHome() as h:
        yield h


@pytest.fixture
def server():
    with FakeOllama([*TAGS, QWEN_TAG], {**SHOW, "qwen3:8b": arch_doc(36, 32, 8, 128, 40960)}) as s:
        yield s


def open_state(home, server, hw=CARD_24GB, roles=""):
    home.config.write_text(f'# my notes\n[models.sources.local]\nkind = "ollama"\nurl = "{server.url}"\n{roles}')
    return tunestate.open_state(hw=hardware.from_dict(hw))


CHAT_ROLE = '[models.roles.chat]\nsource = "local"\nmodel = "qwen3:8b"\n'


def drive(state, script, *, first_run=False, size=SIZE):
    """Runs the app with `script(pilot, app)`; returns (script result, exit status, app)."""
    app = tuneapp.TuneApp(state, LOOK, first_run=first_run)

    async def go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            out = await script(pilot, app)
            await pilot.pause()
            return out

    result = asyncio.run(go())
    return result, app.return_value, app


async def settle(pilot, n=5):
    for _ in range(n):
        await pilot.pause()


def test_first_run_quick_start_previews_each_preset_without_changing_drafts(home, server):
    state = open_state(home, server)

    async def script(pilot, app):
        assert isinstance(app.screen, tuneapp.QuickStart)
        cards = list(app.screen.query(tuneapp.PresetCard))
        assert [c.preset.key for c in cards] == [p.key for p in tuneapp.tunepresets.PRESETS]
        text = " ".join(str(w.render()) for w in cards[0].query("SafeStatic"))
        assert "chat" in text
        return dict(state.drafts)

    drafts, _, _ = drive(state, script, first_run=True)
    assert drafts == {}


def test_choosing_a_preset_card_applies_it_and_marks_changed_roles(home, server):
    state = open_state(home, server)

    async def script(pilot, app):
        await pilot.click("#card-gpu-24")
        await settle(pilot)
        rows = [o.prompt.plain for o in app.query_one("#roles", OptionList)._options]
        return rows, app.query_one("#tabs").get_tab("tab-review").label.plain

    (rows, review), _, _ = drive(state, script, first_run=True)
    assert state.preset.key == "gpu-24" and state.has_changes()
    assert any(r.startswith("chat") and " *" in r for r in rows) and review.startswith("Review ")


def test_quick_start_skip_leaves_without_writing(home, server):
    state = open_state(home, server)
    before = home.config.read_text()

    async def script(pilot, app):
        await pilot.press("escape")

    _, status, _ = drive(state, script, first_run=True)
    assert status == 0 and home.config.read_text() == before


def test_context_slider_drag_updates_gauge_and_release_sets_manual_context(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        gauge = app.query_one(FitGauge)
        before = gauge.caption
        await pilot.mouse_down("#ctx", offset=(1, 0))
        await pilot.hover("#ctx", offset=(5, 0))
        mid_drag = (gauge.caption, state.drafts["chat"].context)
        await pilot.mouse_up("#ctx", offset=(5, 0))
        await settle(pilot)
        return before, mid_drag, app.query_one("#ctx", Slider).value

    (before, (during, draft_during), value), _, _ = drive(state, script)
    assert draft_during == 0  # the draft changes only when the knob is released
    assert during != before
    assert state.drafts["chat"].manual and state.drafts["chat"].context == value


def test_gpu_layers_slider_sets_manual_layers_and_fit(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        slider = app.query_one("#layers", Slider)
        assert slider.high == 36 and slider.auto
        slider.focus()
        await pilot.press("home")
        await settle(pilot)
        caption = app.query_one(FitGauge).caption
        await pilot.press("right", "right")
        await settle(pilot)
        return caption

    caption, _, _ = drive(state, script)
    assert "CPU only" in caption
    assert state.drafts["chat"].gpu_layers == 2 and state.fit("chat").status == "hybrid"


def test_gpu_layers_disabled_with_reason_without_a_gpu(home, server):
    state = open_state(home, server, hw=CPU_ONLY, roles=CHAT_ROLE)

    async def script(pilot, app):
        slider = app.query_one("#layers", Slider)
        return slider.disabled, str(slider.render())

    (disabled, shown), _, _ = drive(state, script)
    assert disabled and "no GPU" in shown


def test_temperature_slider_and_default_button(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        slider = app.query_one("#temp", Slider)
        assert slider.auto  # the model's default
        slider.focus()
        await pilot.press("right")
        await settle(pilot)
        set_to = state.drafts["chat"].temperature
        await pilot.click("#temp-default")
        await settle(pilot)
        return set_to

    set_to, _, _ = drive(state, script)
    assert set_to == pytest.approx(0.75) and state.drafts["chat"].temperature == -1.0


def test_effort_offers_default_and_clears_effort(home, server):
    state = open_state(home, server, roles=CHAT_ROLE + 'effort = "medium"\n')

    async def script(pilot, app):
        effort = app.query_one("#effort", Segmented)
        assert effort.selected == "medium" and effort.segments[0].value == "default"
        start, _, _ = effort.spans()[0]
        await pilot.click("#effort", offset=(start + 1, 0))
        await settle(pilot)

    drive(state, script)
    assert state.drafts["chat"].effort == ""


def test_effort_levels_a_model_cannot_use_are_disabled(home, server):
    state = open_state(home, server, roles='[models.roles.chat]\nsource = "local"\nmodel = "gemma:12b"\n')

    async def script(pilot, app):
        return [s.reason for s in app.query_one("#effort", Segmented).segments]

    reasons, _, _ = drive(state, script)
    assert reasons[0] == "" and all(reasons[1:])


def test_kv_auto_label_names_the_auto_choice(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        return app.query_one("#kv", Segmented).segments[0].label

    label, _, _ = drive(state, script)
    assert label.startswith("auto (") and label.rstrip(")").split("(")[1] in ("f16", "q8_0", "q4_0")


def test_model_picker_lists_usable_models_with_fit(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        await pilot.click("#model-btn")
        await settle(pilot)
        picker = app.screen
        assert isinstance(picker, screens.PickerModal)
        labels = [f"{i.label} {i.note}" for i in picker.items]
        await pilot.press(*"gemma", "enter")
        await settle(pilot)
        return labels

    labels, _, _ = drive(state, script)
    assert any("gemma:12b" in label and any(w in label for w in ("fits", "tight", "CPU")) for label in labels)
    assert state.drafts["chat"].model == "gemma:12b"


def test_ctrl_z_undoes_the_last_change(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        app.query_one("#kv", Segmented).focus()
        await pilot.press("right")
        await settle(pilot)
        changed = state.drafts["chat"].kv_type
        await pilot.press("ctrl+z")
        await settle(pilot)
        return changed

    changed, _, _ = drive(state, script)
    assert changed == "f16" and state.drafts["chat"].kv_type == "" and not state.has_changes()


def test_add_source_form_keeps_text_and_shows_error_inline(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        await pilot.press("f3")
        await settle(pilot)
        await pilot.click("#source-add")
        await settle(pilot)
        modal = app.screen
        kinds = modal.query_one("#kind", Segmented)
        kinds.choose("gguf")
        await pilot.click("#add")  # a gguf source needs a folder
        await settle(pilot)
        error = str(modal.query_one("#error").render())
        return isinstance(app.screen, tuneapp.SourceModal), error

    (still_open, error), _, _ = drive(state, script)
    assert still_open and "needs a folder" in error


def test_remove_source_disabled_with_reason_when_in_use(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        await pilot.click("#--content-tab-tab-sources")
        await settle(pilot)
        return app.query_one("#source-remove", Button).disabled, str(app.query_one("#source-note").render())

    (disabled, note), _, _ = drive(state, script)
    assert disabled and "chat" in note


def test_save_confirms_with_cancel_focused_and_writes_through_apply(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        app.query_one("#kv", Segmented).focus()
        await pilot.press("right")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert app.screen.focused.id == "choice-cancel"
        await pilot.press("enter")  # Enter on the focused Cancel writes nothing
        await settle(pilot)
        unchanged = home.config.read_text()
        await pilot.press("ctrl+s")
        await settle(pilot)
        await pilot.click("#choice-write")
        await settle(pilot, 10)
        await pilot.click("#choice-done")
        await settle(pilot)
        return unchanged

    unchanged, status, app = drive(state, script)
    assert "kv_type" not in unchanged
    chat = tomllib.loads(home.config.read_text())["models"]["roles"]["chat"]
    assert chat["kv_type"] == "f16" and status == 0 and any("wrote" in line for line in app.written)


def test_save_without_changes_exits(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        await pilot.press("ctrl+s")

    _, status, _ = drive(state, script)
    assert status == 0


def test_quit_with_changes_asks_and_defaults_to_keep_editing(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        app.query_one("#kv", Segmented).focus()
        await pilot.press("right")
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen.focused.id == "choice-keep"
        await pilot.press("enter")
        await settle(pilot)
        kept = app.is_running
        await pilot.press("escape")
        await settle(pilot)
        await pilot.click("#choice-discard")
        await settle(pilot)
        return kept

    kept, status, app = drive(state, script)
    assert kept and status == 0 and app.left_unsaved and "kv_type" not in home.config.read_text()


def test_hal_set_role_is_read_only_with_reason(home, server):
    state = open_state(home, server)
    home.config.write_text(home.config.read_text() + '[hal]\ncode_model = "llamacpp/hal-code"\n')

    async def script(pilot, app):
        app.query_one("#roles", OptionList).highlighted = 2  # code
        await settle(pilot)
        row = app.query_one("#roles", OptionList)._options[2].prompt.plain
        return row, bool(app.query("#ctx"))

    (row, has_slider), _, _ = drive(state, script)
    assert "llamacpp/hal-code" in row and "set in [hal]" in row and not has_slider


def test_guard_row_shows_safety_check_cpu(home, server):
    state = open_state(home, server)
    policy = home.config.with_name("policy.toml")
    policy.write_text('schema_version = 1\n\n[policy]\nmodel = "ollama/granite4.1-guardian:8b"\nmodel_format = "guardian"\ngpu = false\n')
    policy.chmod(0o600)

    async def script(pilot, app):
        return app.query_one("#roles", OptionList)._options[-1].prompt.plain

    row, _, _ = drive(state, script)
    assert row.startswith("guard") and "safety check, CPU" in row


def test_busy_warning_is_shown(home, server):
    busy = {**CARD_24GB, "gpus": [{**CARD_24GB["gpus"][0], "free_mib": 2000}]}
    state = open_state(home, server, hw=busy)

    async def script(pilot, app):
        return str(app.query_one("#hw").render())

    shown, _, _ = drive(state, script)
    assert "another program is using" in shown


def test_real_model_labels_show_base_and_tag(home):
    tag = {"name": "hal-chat:latest", "size": 7000 * 2**20, "digest": "d" * 64,
           "details": {"family": "gemma3", "parent_model": "gemma3:12b"}}
    with FakeOllama([tag], {"hal-chat:latest": arch_doc(48, 32, 8, 128, 131072)}) as srv:
        state = open_state(home, srv, roles='[models.roles.chat]\nsource = "local"\nmodel = "hal-chat:latest"\n')

        async def script(pilot, app):
            return app.query_one("#roles", OptionList)._options[0].prompt.plain

        row, _, _ = drive(state, script)
    assert "local/gemma3:12b (hal-chat)" in row


def test_layout_fits_80_by_24(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        return app.query_one("#ctx", Slider).region.right, app.query_one("#temp-default").region.right

    (slider_right, button_right), _, _ = drive(state, script, size=(80, 24))
    assert slider_right <= 80 and button_right <= 80


def test_textual_ctrl_q_also_asks_when_there_are_unsaved_changes(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        app.query_one("#kv", Segmented).focus()
        await pilot.press("right")
        await app.action_quit()  # what Textual's own Ctrl+Q binding calls
        await settle(pilot)
        return isinstance(app.screen, screens.ChoiceModal), app.is_running

    (asked, running), _, _ = drive(state, script)
    assert asked and running


def test_without_a_source_the_app_opens_on_the_sources_tab(home):
    home.config.write_text("# empty\n")
    state = tunestate.open_state(hw=hardware.from_dict(CARD_24GB))

    async def script(pilot, app):
        await settle(pilot)
        return app.query_one("#tabs").active, app.focused.id

    (tab, focused), _, _ = drive(state, script, first_run=True)
    assert tab == "tab-sources" and focused == "sources"


def test_ctrl_c_in_a_dialog_does_not_quit_the_app(home, server):
    state = open_state(home, server, roles=CHAT_ROLE)

    async def script(pilot, app):
        await pilot.press("f3")
        await settle(pilot)
        await pilot.click("#source-add")
        await settle(pilot)
        await pilot.press("ctrl+c")
        await settle(pilot)
        return isinstance(app.screen, tuneapp.SourceModal), app.is_running

    assert drive(state, script)[0] == (True, True)

"""Tests for `halagent.eval.scenarios` and `report` with a scripted runner (REQ-AGT-93, 97)."""

from datetime import date
from pathlib import Path

import pytest

from halagent.eval.report import compare, load_report, run_scenarios
from halagent.eval.scenarios import Case, ScenarioError, ToolUse, TurnOutcome, check, fill, load_cases

CASE_FILE = Path(__file__).parents[3] / "eval" / "assistant" / "scenarios.toml"
MONDAY = date(2026, 10, 26)


class ScriptedRunner:
    """Returns canned outcomes by case id; `state` answers the `after` probes."""

    def __init__(self, outcomes, state=""):
        self.outcomes, self.state, self.probes = outcomes, state, []

    def run_case(self, case, *, model, seed):
        out = self.outcomes[case.id]
        if isinstance(out, Exception):
            raise out
        return out

    def probe(self, tool, args):
        self.probes.append((tool, dict(args)))
        return self.state


def turn(reply="ok", tools=(), asked=()):
    return TurnOutcome(reply, [ToolUse(n, a) for n, a in tools], list(asked))


def test_shipped_case_file_has_50_cases_12_live_and_is_ascii():
    cases = load_cases(CASE_FILE)
    assert (len(cases), sum(c.live for c in cases)) == (50, 12)
    CASE_FILE.read_bytes().decode("ascii")


def test_unknown_key_in_a_case_fails_naming_it(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text('[[case]]\nid = "x"\nask = "hi"\nbogus = 1\n')
    with pytest.raises(ScenarioError, match="x: unknown key bogus"):
        load_cases(path)


def test_ask_becomes_a_single_turn(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text('[[case]]\nid = "x"\nask = "hi"\n')
    assert load_cases(path)[0].turns == ("hi",)


def test_date_placeholders_resolve_against_today():
    assert fill("{date:thursday}T10", MONDAY) == "2026-10-29T10"
    assert fill("{date:tomorrow} {weekday:today} {day:friday}", MONDAY) == "2026-10-27 Monday 30"
    assert fill("{date:monday}", MONDAY) == "2026-11-02"


def test_missing_required_tool_and_forbidden_tool_are_reported():
    case = Case("c", ("hi",), tools=("calendar_add",), forbid=("shell",))
    problems = check(case, [turn(tools=[("shell", {})])], ScriptedRunner({}))
    assert problems == ["did not call calendar_add", "called shell, which it must not"]


def test_args_are_matched_by_regex_on_the_made_calls():
    case = Case("c", ("hi",), args={"calendar_add": {"title": "(?i)dentist"}})
    bad = check(case, [turn(tools=[("calendar_add", {"title": "lunch"})])], ScriptedRunner({}))
    good = check(case, [turn(tools=[("calendar_add", {"title": "Dentist"})])], ScriptedRunner({}))
    assert len(bad) == 1 and good == []


def test_confirm_first_passes_only_when_the_broker_asked_for_approval():
    case = Case("c", ("hi",), confirm_first=("calendar_delete",))
    runner = ScriptedRunner({})
    assert check(case, [turn(tools=[("calendar_delete", {})])], runner)
    assert check(case, [turn(tools=[("calendar_delete", {})], asked=["calendar_delete"])], runner) == []


def test_text_yes_in_a_later_turn_does_not_count_as_approval():
    case = Case("c", ("delete it", "Yes, go ahead."), confirm_first=("calendar_delete",))
    assert check(case, [turn("sure?"), turn("done")], ScriptedRunner({}))


def test_reply_checks_and_length_cap():
    case = Case("c", ("hi",), reply="(?i)hello", reply_not="sorry", max_chars=10)
    assert check(case, [turn("Hello")], ScriptedRunner({})) == []
    assert len(check(case, [turn("I am sorry, long reply")], ScriptedRunner({}))) == 3


def test_after_steps_probe_host_state_unless_no_extensions():
    case = Case("c", ("hi",), after=({"tool": "find", "args": {"q": "x"}, "match": "dentist"},))
    assert check(case, [turn()], ScriptedRunner({}, state="dentist 10:00")) == []
    assert check(case, [turn()], ScriptedRunner({}, state="nothing"))
    runner = ScriptedRunner({}, state="")
    assert check(Case("c", ("hi",), after=case.after, no_extensions=True), [turn()], runner) == []
    assert runner.probes == []


def test_run_scenarios_writes_a_report_with_wilson_interval(tmp_path):
    cases = [Case("a", ("hi",), reply="ok"), Case("b", ("hi",), reply="never"), Case("c", ("hi",))]
    runner = ScriptedRunner({"a": [turn()], "b": [turn()], "c": RuntimeError("boom")})
    report = run_scenarios(cases, runner, model="m", repeat=2, out_dir=tmp_path)
    assert [a.passed for a in report.attempts] == [True, True, False, False, False, False]
    assert report.attempts[-1].problems == ["error: boom"]
    again = load_report(tmp_path / "report.json")
    assert again.summary() == report.summary() and report.summary()["total"] == 6
    assert report.at.endswith("Z")


def _report(model, passes):
    from halagent.eval.report import Attempt, Report

    return Report(model, "2026-10-30T00:00:00Z", [Attempt(f"c{i}", 0, p, []) for i, p in enumerate(passes)])


def test_comparison_pairs_on_case_and_repeat_and_names_a_winner_only_by_the_rule():
    a = _report("a", [True] * 8 + [False] * 2)
    b = _report("b", [False] * 6 + [True] * 2 + [False] * 2)
    result = compare(a, b)
    assert (result["b_only_a_passes"], result["c_only_b_passes"], result["winner"]) == (6, 0, "a")
    close = compare(_report("a", [True, False, True]), _report("b", [True, True, False]))
    assert close["winner"] == "no detectable difference"


def test_comparison_of_unrelated_reports_fails_loudly():
    from halagent.eval.report import Attempt, Report

    other = Report("x", "t", [Attempt("zzz", 0, True, [])])
    with pytest.raises(ValueError, match="share no"):
        compare(_report("a", [True]), other)

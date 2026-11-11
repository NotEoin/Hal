"""Tests for `halcore.security` (`hal status` and `hal doctor` lines for I2 and I5)."""

import pytest

from halcore import security
from halkit import llm
from halkit.testing import FakeModelServer, TempHalHome


@pytest.fixture
def home(monkeypatch):
    monkeypatch.setattr(llm, "_gateway_installed", lambda: True)
    monkeypatch.setattr(security, "docker_runtimes", lambda: {"runc", "runsc"})
    with TempHalHome() as h:
        yield h


def set_lockdown(home, level):
    home.config.write_text(f'[sandbox]\nlockdown = "{level}"\n')


def test_default_report_shows_standard_lockdown_and_balanced_paranoia(home):
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.models = ["hal-classifier"]
        report = security.collect(check_runtime=True)
    assert report.lines[:2] == ["lockdown: standard", "paranoia: balanced"]
    assert report.lines[2] == "classifier: reachable (hal-classifier)"
    assert report.problems == [] and report.warnings == []


def test_strict_lockdown_raises_paranoia_to_high_and_says_so(home):
    set_lockdown(home, "strict")
    report = security.collect(check_runtime=False)
    assert report.lines[1] == "paranoia: high, raised from balanced by lockdown strict"


def test_an_unreachable_classifier_warns_that_every_call_asks(home):
    report = security.collect(check_runtime=False)
    assert "classifier: UNREACHABLE (hal-classifier)" in report.lines
    assert "every boxed call counts as high risk and asks" in report.warnings[0]


def test_lockdown_off_is_a_problem_and_doctor_exits_nonzero(home, capsys):
    set_lockdown(home, "off")
    assert security.main(["doctor"]) == 1
    assert capsys.readouterr().out.startswith("PROBLEM: lockdown is off")


def test_strict_without_runsc_is_a_problem_and_standard_is_a_warning(home, monkeypatch):
    monkeypatch.setattr(security, "docker_runtimes", lambda: {"runc"})
    set_lockdown(home, "strict")
    assert "lockdown strict refuses to start" in security.collect(check_runtime=True).problems[0]
    set_lockdown(home, "standard")
    report = security.collect(check_runtime=True)
    assert report.problems == [] and "share the host kernel" in report.warnings[0]


def test_status_never_fails_and_does_not_probe_docker(home, monkeypatch, capsys):
    def boom():
        raise AssertionError("docker was queried")

    monkeypatch.setattr(security, "docker_runtimes", boom)
    set_lockdown(home, "off")
    assert security.main(["status"]) == 0
    assert "lockdown: off" in capsys.readouterr().out


def test_paranoia_off_reports_the_classifier_off(home):
    home.policy.write_text('schema_version = 1\n[policy]\nparanoia = "off"\n')
    home.policy.chmod(0o600)
    assert "classifier: off" in security.collect(check_runtime=False).lines


def test_unknown_subcommand_is_a_usage_error(home):
    assert security.main(["bogus"]) == 2


def test_an_unpinned_ollama_classifier_model_is_a_warning(home):
    # regression: doctor said "reachable" while the gateway refused the unpinned Guardian model
    # with 503 model_unverified, so every call asked for approval
    home.policy.write_text('schema_version = 1\n[policy]\nmodel = "ollama/guard:8b"\n')
    home.policy.chmod(0o600)
    with FakeModelServer(home.runtime / "gateway.sock") as server:
        server.models = ["ollama/guard:8b"]
        report = security.collect(check_runtime=False)
        assert any("guard:8b is not pinned in models.toml" in w for w in report.warnings)
        home.config.with_name("models.toml").write_text(
            'schema = 1\n\n[[model]]\nname = "guard:8b"\nsource = "ollama:guard:8b@sha256:' + "a" * 64 + '"\n'
        )
        assert not any("not pinned" in w for w in security.collect(check_runtime=False).warnings)

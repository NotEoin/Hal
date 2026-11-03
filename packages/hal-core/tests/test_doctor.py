"""Tests for `halcore.doctor` (`hal doctor`)."""

from halcore import cli, doctor
from halkit.testing import TempHalHome


def test_runtime_dir_open_to_others_fails_with_the_chmod_fix(monkeypatch, tmp_path):
    run = tmp_path / "hal"
    run.mkdir()
    run.chmod(0o755)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(run))
    check = doctor.runtime_dir_check()
    assert check.status == "fail"
    assert f"chmod 700 {run}" in check.detail and "0755" in check.detail


def test_private_runtime_dir_passes(monkeypatch, tmp_path):
    run = tmp_path / "hal"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(run))
    assert doctor.runtime_dir_check().status == "ok"


def test_missing_docker_fails_with_an_install_hint(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    check = doctor.docker_check()
    assert check.status == "fail" and "install" in check.detail


def test_unreachable_model_server_warns_and_does_not_raise(monkeypatch):
    def boom(*, timeout):
        raise doctor.llm.ModelUnavailable("down")

    monkeypatch.setattr(doctor.llm, "ping", boom)
    assert doctor.model_check().status == "warn"


def test_a_crashing_sandbox_probe_becomes_a_fail_line(monkeypatch):
    def boom():
        raise RuntimeError("probe broke")

    with TempHalHome():
        monkeypatch.setattr(doctor.sandbox_doctor, "run_checks", boom)
        monkeypatch.setattr(doctor, "security_checks", lambda: [])
        monkeypatch.setattr(doctor, "model_check", lambda: doctor.Check("model server", "ok", "x"))
        names = {c.name: c for c in doctor.collect()}
    assert names["sandbox"].status == "fail"


def test_doctor_and_status_are_commands():
    assert cli.resolve(["doctor"])[0].module == "doctor"
    assert cli.resolve(["status"])[0].module == "doctor"


def test_render_marks_failures():
    text = doctor.render([doctor.Check("a", "ok", "fine"), doctor.Check("bb", "fail", "bad")])
    assert "[ok  ] a " in text and "[FAIL] bb" in text


def test_only_keeps_checks_whose_name_starts_with_the_filter():
    checks = [doctor.Check("docker", "ok", ""), doctor.Check("runtime dir", "ok", ""), doctor.Check("security", "ok", "")]
    assert [c.name for c in doctor.select(checks, ["Run", "doc"])] == ["docker", "runtime dir"]
    assert doctor.select(checks, []) == checks


def test_fix_sets_own_runtime_dir_to_0700(monkeypatch, tmp_path):
    run = tmp_path / "hal"
    run.mkdir()
    run.chmod(0o755)
    monkeypatch.setenv("HAL_RUNTIME_DIR", str(run))
    assert "set" in doctor.fix_runtime_dir()
    assert doctor.runtime_dir_check().status == "ok"


def test_only_with_no_match_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "collect", lambda: [doctor.Check("docker", "ok", "")])
    assert doctor.main(["--only", "nothing"]) == 2
    assert "no check matches" in capsys.readouterr().err


def test_units_rendering_outside_the_packs_warn_with_the_fix(monkeypatch):
    monkeypatch.setattr(doctor.units, "outside_packs", lambda: ["/old/hal-ecosystem/clock"])
    monkeypatch.setattr(doctor.units, "missing_exec_paths", lambda: [])
    monkeypatch.setattr(doctor.units, "failing", lambda: [])
    check = doctor.units_check()
    assert check.status == "warn" and "/old/hal-ecosystem/clock" in check.detail and "hal setup" in check.detail


def test_units_check_is_ok_when_every_unit_comes_from_a_pack(monkeypatch):
    monkeypatch.setattr(doctor.units, "outside_packs", lambda: [])
    monkeypatch.setattr(doctor.units, "missing_exec_paths", lambda: [])
    monkeypatch.setattr(doctor.units, "failing", lambda: [])
    assert doctor.units_check().status == "ok"


def test_a_crash_looping_unit_warns_with_the_journal_command(monkeypatch):
    """Regression: hal-telegram restarted 186 times while doctor said every unit was fine."""
    monkeypatch.setattr(doctor.units, "outside_packs", lambda: [])
    monkeypatch.setattr(doctor.units, "missing_exec_paths", lambda: [])
    monkeypatch.setattr(doctor.units, "failing", lambda: ["hal-telegram.service"])
    check = doctor.units_check()
    assert check.status == "warn" and "hal-telegram.service" in check.detail
    assert "journalctl --user -u hal-telegram.service" in check.detail


def test_failing_reads_failed_and_restarting_units_from_systemctl(monkeypatch):
    shown = (
        "Id=hal-clock.service\nActiveState=active\nSubState=running\nNRestarts=0\n\n"
        "Id=hal-telegram.service\nActiveState=activating\nSubState=auto-restart\nNRestarts=186\n\n"
        "Id=hal-jobs.service\nActiveState=failed\nSubState=failed\nNRestarts=0\n\n"
        "Id=hal-jobs.timer\nActiveState=active\nSubState=waiting\nNRestarts=\n"
    )
    names = ["hal-clock.service", "hal-telegram.service", "hal-jobs.service", "hal-jobs.timer"]
    asked = []

    def show(*args, **kwargs):
        asked.extend(args)
        return doctor.units.subprocess.CompletedProcess(args, 0, shown, "")

    monkeypatch.setattr(doctor.units, "installed", lambda: dict.fromkeys([*names, "hal-briefing@.service"], "/m"))
    monkeypatch.setattr(doctor.units, "systemctl", show)
    assert doctor.units.failing() == ["hal-jobs.service", "hal-telegram.service"]
    assert "hal-briefing@.service" not in asked  # systemctl refuses the whole call for a template


def test_doctor_warns_when_no_model_is_assigned_and_names_hal_tune(monkeypatch, tmp_path):
    monkeypatch.setenv("HAL_CONFIG", str(tmp_path / "config.toml"))
    check = doctor.models_check()
    assert check.status == doctor.WARN and "hal tune --auto --write" in check.detail


def test_doctor_reports_the_assigned_chat_model(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models.sources.l]\nkind = "gguf"\npath = "/m"\n[models.roles.chat]\nsource = "l"\nmodel = "m"\n')
    monkeypatch.setenv("HAL_CONFIG", str(cfg))
    check = doctor.models_check()
    assert check.status == doctor.OK and "l/m" in check.detail


def test_wsl2_without_systemd_warns_with_the_wsl_conf_fix(tmp_path):
    checks = {c.name: c for c in doctor.wsl_checks(wsl2=True, systemd_dir=tmp_path / "none", cuda_lib=tmp_path / "none")}
    assert checks["platform"].status == "ok"
    assert checks["wsl systemd"].status == "warn" and "systemd=true" in checks["wsl systemd"].detail
    assert checks["wsl gpu"].status == "warn" and "NVIDIA driver" in checks["wsl gpu"].detail


def test_wsl2_with_systemd_and_cuda_passes(tmp_path):
    lib = tmp_path / "libcuda.so.1"
    lib.write_text("")
    checks = doctor.wsl_checks(wsl2=True, systemd_dir=tmp_path, cuda_lib=lib)
    assert [c.status for c in checks] == ["ok", "ok", "ok"]


def test_other_hosts_get_no_wsl_lines(tmp_path):
    assert doctor.wsl_checks(wsl2=False) == []

"""Tests for the server checks SC01 to SC22 (REQ-RMT-14, 15, 41)."""

from __future__ import annotations

import os

import pytest

from halremote import authkeys, keys, servercheck
from halremote.model import Forward

from .conftest import FakeRunner

GOOD_SSHD = """\
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers alice
AllowTcpForwarding local
AllowStreamLocalForwarding no
PermitUserEnvironment no
"""
SS_OK = "LISTEN 0 4096 127.0.0.1:8741 0.0.0.0:*\nLISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY = keys.PublicKey("ssh-ed25519", BLOB, "")


@pytest.fixture
def desk(tmp_path):
    """A passing desktop: ssh config, home with strict modes, fake sshd and tools."""
    etc = tmp_path / "etc-ssh"
    etc.mkdir()
    (etc / "sshd_config").write_text(GOOD_SSHD)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    os.chmod(home, 0o755)
    os.chmod(home / ".ssh", 0o700)
    akeys = home / ".ssh" / "authorized_keys"
    akeys.write_text("")
    os.chmod(akeys, 0o600)
    sshd = tmp_path / "sshd"
    sshd.write_text("#!/bin/sh\n")
    sshd.chmod(0o755)
    runner = FakeRunner(
        {"systemctl": (0, "active\ninactive\n"), "ss": (0, SS_OK), "ufw": (0, "Status: active\n")}
    )
    return {
        "home": home,
        "etc_ssh": etc,
        "runner": runner,
        "user": "alice",
        "uid": os.getuid(),
        "sshd_path": sshd,
        "http_status": lambda url, timeout: 401,
        "now": 1_000_000.0,
    }


def run(desk, **over):
    return {r.id: r for r in servercheck.run_checks(**{**desk, **over})}


def test_a_hardened_desktop_passes_every_critical_check(desk):
    res = run(desk)
    assert not [r.id for r in res.values() if r.severity == "critical" and r.status not in ("pass", "skip")]
    assert servercheck.has_critical_failure(res.values()) is False


def test_results_cover_every_id(desk):
    assert sorted(run(desk)) == [f"SC{n:02d}" for n in range(1, 23)]


def test_missing_sshd_fails_sc01(desk, tmp_path):
    assert run(desk, sshd_path=tmp_path / "nope")["SC01"].status == "fail"


@pytest.mark.parametrize(
    ("out", "status"), [((0, "inactive\nactive\n"), "pass"), ((3, "inactive\ninactive\n"), "fail")]
)
def test_ssh_service_state_decides_sc02(desk, out, status):
    desk["runner"].outputs["systemctl"] = out
    assert run(desk)["SC02"].status == status


def test_missing_systemctl_is_unknown_for_sc02(desk):
    desk["runner"].outputs["systemctl"] = None
    assert run(desk)["SC02"].status == "unknown"


@pytest.mark.parametrize(
    ("cid", "line", "bad"),
    [
        ("SC03", "PasswordAuthentication", "yes"),
        ("SC04", "KbdInteractiveAuthentication", "yes"),
        ("SC05", "PermitRootLogin", "yes"),
        ("SC07", "AllowTcpForwarding", "yes"),
        ("SC08", "AllowStreamLocalForwarding", "yes"),
        ("SC09", "PermitUserEnvironment", "yes"),
    ],
)
def test_a_weak_sshd_keyword_fails_its_check(desk, cid, line, bad):
    cfg = desk["etc_ssh"] / "sshd_config"
    cfg.write_text(
        "".join(
            f"{line} {bad}\n" if ln.startswith(line + " ") else ln + "\n" for ln in GOOD_SSHD.splitlines()
        )
    )
    assert run(desk)[cid].status == "fail"


def test_unset_password_authentication_defaults_to_yes_and_fails_sc03(desk):
    (desk["etc_ssh"] / "sshd_config").write_text("AllowUsers alice\n")
    assert run(desk)["SC03"].status == "fail"


def test_allow_users_without_the_user_fails_sc06(desk):
    (desk["etc_ssh"] / "sshd_config").write_text(GOOD_SSHD.replace("alice", "bob"))
    assert run(desk)["SC06"].status == "fail"


def test_unreadable_sshd_config_makes_sshd_checks_unknown(desk):
    (desk["etc_ssh"] / "sshd_config").unlink()
    res = run(desk)
    assert res["SC03"].status == "unknown"
    assert res["SC06"].status == "unknown"


def test_authorized_keys_file_without_the_default_fails_sc10(desk):
    cfg = desk["etc_ssh"] / "sshd_config"
    cfg.write_text(GOOD_SSHD + "AuthorizedKeysFile /srv/keys/%u\n")
    assert run(desk)["SC10"].status == "fail"


@pytest.mark.parametrize("target", ["home", "ssh", "akeys"])
def test_loose_modes_fail_sc11(desk, target):
    path = {
        "home": desk["home"],
        "ssh": desk["home"] / ".ssh",
        "akeys": desk["home"] / ".ssh/authorized_keys",
    }[target]
    os.chmod(path, 0o777 if target == "home" else 0o666)
    assert run(desk)["SC11"].status == "fail"


def test_foreign_owner_fails_sc11(desk):
    assert run(desk, uid=desk["uid"] + 1)["SC11"].status == "fail"


def _line(role, tmp_path, *, serve=None, hub=None, from_pattern="192.168.1.0/24"):
    exe = tmp_path / "bin"
    exe.mkdir(exist_ok=True)
    for name in ("hal-remote", "hal-hub"):
        (exe / name).write_text("#!/bin/sh\n")
        (exe / name).chmod(0o755)
    return authkeys.render_line(
        role,
        client="laptop",
        key=KEY,
        from_pattern=from_pattern,
        forwards=[Forward("gateway", 8741, 8741)],
        serve_path=serve or exe / "hal-remote",
        hub_path=hub or exe / "hal-hub",
    )


def _write_keys(desk, *lines):
    path = desk["home"] / ".ssh/authorized_keys"
    path.write_text("\n".join(lines) + "\n")


def test_well_formed_hal_lines_pass_sc12_and_sc13(desk, tmp_path):
    _write_keys(desk, _line("tunnel", tmp_path), _line("mcp", tmp_path))
    res = run(desk)
    assert (res["SC12"].status, res["SC13"].status) == ("pass", "pass")


def test_line_without_from_fails_sc12_unless_registry_says_any(desk, tmp_path):
    _write_keys(desk, _line("mcp", tmp_path, from_pattern=None))
    assert run(desk)["SC12"].status == "fail"
    assert run(desk, registry_from_any={"laptop"})["SC12"].status == "pass"


def test_tunnel_line_without_permitlisten_fails_sc12(desk, tmp_path):
    _write_keys(desk, _line("tunnel", tmp_path).replace(',permitlisten="127.0.0.1:1"', ""))
    assert run(desk)["SC12"].status == "fail"


def test_mcp_line_with_port_forwarding_fails_sc12(desk, tmp_path):
    _write_keys(desk, _line("mcp", tmp_path).replace("restrict,", "restrict,port-forwarding,"))
    assert run(desk)["SC12"].status == "fail"


def test_missing_forced_command_binary_fails_sc13(desk, tmp_path):
    _write_keys(desk, _line("mcp", tmp_path, hub=tmp_path / "gone"))
    assert run(desk)["SC13"].status == "fail"


def test_sshrc_fails_sc15(desk):
    (desk["etc_ssh"] / "sshrc").write_text("echo hi\n")
    assert run(desk)["SC15"].status == "fail"


def test_exposed_ports_lists_only_hal_ports_off_loopback():
    out = (
        "LISTEN 0 1 0.0.0.0:8741 0.0.0.0:*\n"
        "LISTEN 0 1 [::]:11434 [::]:*\n"
        "LISTEN 0 1 [::1]:8740 *:*\n"
        "LISTEN 0 1 0.0.0.0:22 *:*\n"
    )
    assert servercheck.exposed_ports(out) == ["0.0.0.0:8741", "[::]:11434"]


def test_hal_port_on_all_interfaces_fails_sc16(desk):
    desk["runner"].outputs["ss"] = (0, "LISTEN 0 1 0.0.0.0:8742 0.0.0.0:*\n")
    assert run(desk)["SC16"].status == "fail"


@pytest.mark.parametrize(("code", "status"), [(401, "pass"), (200, "fail"), (None, "fail")])
def test_gateway_probe_must_answer_401_for_sc17(desk, code, status):
    assert run(desk, http_status=lambda url, timeout: code)["SC17"].status == status


def test_match_block_marks_sc18_and_unsettles_keywords(desk):
    (desk["etc_ssh"] / "sshd_config").write_text(GOOD_SSHD + "Match User bob\n  PasswordAuthentication yes\n")
    res = run(desk)
    assert res["SC18"].status == "fail"
    assert res["SC03"].status == "unknown"


def test_registry_and_lines_must_agree_for_sc19(desk, tmp_path):
    _write_keys(desk, _line("mcp", tmp_path))
    assert run(desk, registry_clients={"laptop"})["SC19"].status == "pass"
    assert run(desk, registry_clients={"laptop", "ghost"})["SC19"].status == "fail"
    assert run(desk, registry_clients=set())["SC19"].status == "fail"
    assert run(desk)["SC19"].status == "skip"


def test_unrestricted_key_is_counted_by_sc20(desk):
    _write_keys(desk, f"ssh-ed25519 {BLOB} me@host")
    assert run(desk)["SC20"].detail.startswith("1 ")


def test_inactive_firewall_or_missing_ufw(desk):
    desk["runner"].outputs["ufw"] = (0, "Status: inactive\n")
    assert run(desk)["SC21"].status == "fail"
    desk["runner"].outputs["ufw"] = None
    assert run(desk)["SC21"].status == "skip"


def test_stale_delivery_is_deleted_only_with_fix(desk, tmp_path):
    d = tmp_path / "delivery"
    d.mkdir()
    old = d / "laptop.json"
    old.write_text("{}")
    os.utime(old, (1.0, 1.0))
    assert run(desk, delivery_dir=d)["SC22"].status == "fail"
    assert old.exists()
    assert run(desk, delivery_dir=d, fix=True)["SC22"].status == "pass"
    assert not old.exists()


def test_sshd_t_output_replaces_the_static_reader(desk):
    desk["etc_ssh"].joinpath("sshd_config").write_text("PasswordAuthentication yes\nMatch all\n")
    desk["runner"].outputs["sudo"] = (
        0,
        "passwordauthentication no\nallowusers alice\npermituserenvironment no\n",
    )
    res = run(desk, sshd_t=True)
    assert (res["SC03"].status, res["SC06"].status, res["SC18"].status) == ("pass", "pass", "pass")
    assert desk["runner"].calls[-1][:3] == ["sudo", "-n", str(desk["sshd_path"])] or any(
        c[:2] == ["sudo", "-n"] for c in desk["runner"].calls
    )


def test_no_sudo_call_without_sshd_t(desk):
    run(desk)
    assert not any(c[0] == "sudo" for c in desk["runner"].calls)


def test_accepted_risk_lifts_a_critical_failure():
    bad = servercheck.CheckResult("SC03", "t", "critical", "fail", "", "")
    assert servercheck.has_critical_failure([bad]) is True
    assert servercheck.has_critical_failure([bad], accepted={"SC03"}) is False


def _fake_shell(tmp_path, body):
    shell = tmp_path / "fakesh"
    shell.write_text(f"#!/bin/sh\n{body}\n")
    shell.chmod(0o755)
    return str(shell)


def _sc14(desk, tmp_path, body):
    _write_keys(desk, _line("mcp", tmp_path))
    from halkit import proc

    return run(desk, runner=proc.run, login_shell=_fake_shell(tmp_path, body), http_status=lambda u, t: 401)[
        "SC14"
    ]


def test_forced_command_answering_check_ok_passes_sc14(desk, tmp_path):
    body = "test -S /proc/self/fd/0 || [ -S /dev/stdin ] || exit 9; echo '{\"ok\": true}'"
    assert _sc14(desk, tmp_path, body).status == "pass"


def test_noisy_rc_output_before_the_check_fails_sc14(desk, tmp_path):
    assert _sc14(desk, tmp_path, 'echo "Welcome"; echo \'{"ok": true}\'').status == "fail"


def test_check_reporting_not_ok_fails_sc14(desk, tmp_path):
    assert _sc14(desk, tmp_path, "echo '{\"ok\": false}'").status == "fail"


def test_forced_command_env_is_minimal_for_sc14(desk, tmp_path):
    cond = '[ "$PATH" = /usr/bin:/bin ] && [ "$SSH_ORIGINAL_COMMAND" = check ]'
    body = f'echo "{{\\"ok\\": $( {cond} && echo true || echo false )}}"'
    assert _sc14(desk, tmp_path, body).status == "pass"


def test_sc14_is_skipped_without_an_mcp_key(desk):
    assert run(desk)["SC14"].status == "skip"

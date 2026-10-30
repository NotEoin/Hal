"""Tests for `halremote.sshdconf` on synthetic fixture trees under `fixtures/sshd/<case>/etc/ssh`."""

import os
import shutil
from pathlib import Path

import pytest

from halremote import sshdconf

FIXTURES = Path(__file__).parent / "fixtures" / "sshd"


def _read(case: str) -> sshdconf.SshdConfig:
    return sshdconf.read(FIXTURES / case / "etc" / "ssh")


def test_dropin_wins_over_the_main_file_because_include_comes_first():
    cfg = _read("ubuntu_dropin")
    assert sshdconf.effective(cfg, "PasswordAuthentication", "yes") == "no"
    assert sshdconf.effective(cfg, "x11forwarding", "yes") == "no"
    assert cfg.allow_users == ("alice",)
    assert [p.name for p in cfg.files] == ["sshd_config", "10-hal-hardening.conf"]


def test_unset_key_gets_the_default():
    assert sshdconf.effective(_read("ubuntu_dropin"), "PermitUserEnvironment", "no") == "no"


def test_cloud_init_file_overrides_by_lexical_order_and_users_accumulate():
    cfg = _read("cloud_init_order")
    assert sshdconf.effective(cfg, "passwordauthentication", "yes") == "yes"
    assert cfg.allow_users == ("alice", "bob", "carol")


def test_include_glob_is_sorted_so_a_conf_beats_b_conf():
    assert sshdconf.effective(_read("relative_include"), "permitrootlogin", "") == "prohibit-password"


def test_quoted_arguments_equals_form_and_multi_argument_keys():
    cfg = _read("quoted_and_match")
    assert cfg.values["port"] == ("2222",)
    assert cfg.values["authorizedkeysfile"] == (".ssh/authorized_keys", "/srv/keys/%u")


def test_match_block_marks_its_keys_unknown_and_match_all_ends_it():
    cfg = _read("quoted_and_match")
    assert cfg.conditional == {"passwordauthentication", "x11forwarding"}
    assert sshdconf.effective(cfg, "PasswordAuthentication", "yes") is None
    assert sshdconf.effective(cfg, "PermitRootLogin", "") == "no"


def test_include_inside_a_match_block_is_conditional_and_ends_with_its_file():
    cfg = _read("include_in_match")
    assert "allowtcpforwarding" in cfg.conditional
    assert sshdconf.effective(cfg, "permitrootlogin", "") == "no"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
def test_unreadable_file_makes_every_answer_unknown(tmp_path):
    shutil.copytree(FIXTURES / "unreadable", tmp_path / "t")
    secret = tmp_path / "t" / "etc" / "ssh" / "sshd_config.d" / "10-secret.conf"
    secret.chmod(0)
    try:
        cfg = sshdconf.read(tmp_path / "t" / "etc" / "ssh")
    finally:
        secret.chmod(0o600)
    assert cfg.unreadable == (secret,)
    assert sshdconf.effective(cfg, "PasswordAuthentication", "yes") is None


def test_missing_main_file_is_reported_unreadable(tmp_path):
    cfg = sshdconf.read(tmp_path)
    assert cfg.unreadable == (tmp_path / "sshd_config",)
    assert sshdconf.effective(cfg, "port", "22") is None


def test_include_loop_stops_at_the_depth_limit(tmp_path):
    (tmp_path / "sshd_config").write_text("Include sshd_config\nPort 1\n")
    cfg = sshdconf.read(tmp_path)
    assert cfg.values["port"] == ("1",)
    assert len(cfg.files) == sshdconf.MAX_INCLUDE_DEPTH + 1

"""Tests for `halkit.notify`: quiet hours, do-not-disturb, channels and history, all channels stubbed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from halkit import netguard, notify, proc
from halkit.testing import FakeResolver, TempHalHome
from halkit.timeutil import iso_z

NOON = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CONFIG = '[notify]\nntfy_topic = "t"\nquiet_from = 22\nquiet_until = 8\nchannels = ["desktop", "phone"]\n'


@pytest.fixture
def home(monkeypatch):
    with TempHalHome() as h:
        h.config.parent.mkdir(parents=True, exist_ok=True)
        h.config.write_text(CONFIG)
        yield h


@pytest.fixture
def calls(monkeypatch):
    """Replaces every channel with a recorder that succeeds."""
    log: list[tuple] = []
    monkeypatch.setattr(
        notify, "_desktop", lambda title, body, urgent: log.append(("desktop", urgent)) or True
    )
    monkeypatch.setattr(
        notify, "_ntfy", lambda title, body, urgent, silent, url="": log.append(("phone", silent)) or True
    )
    monkeypatch.setattr(notify, "_speak", lambda title, body: log.append(("voice",)) or True)
    return log


def test_dnd_silences_the_phone_and_voice_but_urgent_breaks_through(home, calls):
    notify.dnd(30)
    out = notify.send("hi", "there", speak=True)
    assert out == {"reached": ["desktop", "phone"], "quiet": True}
    assert ("phone", True) in calls and ("voice",) not in calls
    calls.clear()
    notify.send("fire", "now", urgent=True, speak=True)
    assert ("phone", False) in calls and ("voice",) in calls


def test_dnd_zero_turns_it_off_and_state_reports_the_end_in_utc(home):
    state = notify.dnd(30)
    assert str(state["dnd_until"]).endswith("Z")
    assert notify.dnd(0)["dnd_until"] is None


def test_an_expired_dnd_file_is_removed(home):
    (notify._dir() / "dnd").write_text(iso_z(NOON - timedelta(minutes=1)))
    assert notify.state()["dnd_until"] is None and not (notify._dir() / "dnd").exists()


def test_a_malformed_dnd_file_is_ignored_and_removed(home):
    (notify._dir() / "dnd").write_text("garbage")
    assert notify.state()["dnd_until"] is None and not (notify._dir() / "dnd").exists()


@pytest.mark.parametrize(("hour", "expected"), [(23, True), (3, True), (8, False), (12, False), (21, False)])
def test_quiet_hours_wrap_midnight(home, hour, expected):
    assert notify.quiet(NOON.replace(hour=hour)) is expected


def test_quiet_hours_inside_one_day(home):
    home.config.write_text("[notify]\nquiet_from = 13\nquiet_until = 15\n")
    assert notify.quiet(NOON.replace(hour=14)) and not notify.quiet(NOON.replace(hour=16))


def test_history_keeps_newest_first_with_channel_lists(home, calls):
    notify.send("first", "a", channels=["desktop"])
    notify.send("second", "b", channels=["desktop", "phone"])
    rows = notify.history(5)
    assert [r["title"] for r in rows] == ["second", "first"]
    assert rows[0]["channels"] == ["desktop", "phone"] and rows[0]["urgent"] is False


def test_history_is_cut_to_the_cap(home, calls, monkeypatch):
    monkeypatch.setattr(notify, "MAX_HISTORY", 3)
    for i in range(5):
        notify.send(f"n{i}", channels=["desktop"])
    assert len(notify.history(10)) == 3


def test_unknown_channel_is_refused_before_anything_is_sent(home, calls):
    with pytest.raises(ValueError, match="unknown notification channels"):
        notify.send("x", channels=["desktop", "pigeon"])
    assert calls == []


def test_a_failing_channel_is_left_out_not_raised(home, monkeypatch):
    monkeypatch.setattr(notify, "_desktop", lambda *a: False)
    monkeypatch.setattr(notify, "_ntfy", lambda *a, **k: True)
    assert notify.send("x")["reached"] == ["phone"]


def test_desktop_uses_notify_send_with_urgency_and_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, *, timeout, **kw):
        seen.update(argv=list(argv), timeout=timeout)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(proc, "run", fake_run)
    assert notify._desktop("T", "B", True)
    assert seen["argv"] == ["notify-send", "-a", "Hal", "-u", "critical", "T", "B"] and seen["timeout"] == 5.0


def test_desktop_failure_returns_false(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(proc, "run", boom)
    assert notify._desktop("T", "B", False) is False


def test_speech_runs_the_configured_command_with_the_text_as_last_argument(home, monkeypatch):
    home.config.write_text('[notify]\nsay_command = "say --voice hal"\n')
    seen = {}
    monkeypatch.setattr(
        proc, "run", lambda argv, **k: seen.update(argv=list(argv)) or type("R", (), {"returncode": 0})()
    )
    assert notify._speak("Title", "body") and seen["argv"] == ["say", "--voice", "hal", "Title. body"]


def test_speech_with_no_command_is_off(home):
    assert notify._speak("a", "b") is False


def test_push_posts_to_the_topic_through_the_egress_guard(home, monkeypatch):
    seen = {}

    def fake_fetch(url, **kw):
        seen.update(url=url, **kw)
        return type("Resp", (), {"status": 200})()

    monkeypatch.setattr(netguard, "fetch", fake_fetch)
    assert notify._ntfy("Caf\u00e9", "body", urgent=True, silent=False, url="https://example.org/x")
    assert seen["url"] == "https://ntfy.sh/t" and seen["domains"] == ("ntfy.sh",) and seen["method"] == "POST"
    assert seen["headers"]["Priority"] == "high" and seen["headers"]["Click"] == "https://example.org/x"
    assert seen["timeout"] == 10.0 and seen["body"] == b"body"


def test_push_with_no_topic_is_off(home):
    home.config.write_text("[notify]\n")
    assert notify._ntfy("a", "b", False, False) is False


def test_push_refuses_plain_http_without_resolving_the_host(home, monkeypatch):
    home.config.write_text('[notify]\nntfy_topic = "t"\nntfy_server = "http://ntfy.example"\n')
    resolver = FakeResolver({"ntfy.example": ["93.184.216.34"]})
    real = netguard.fetch
    monkeypatch.setattr(netguard, "fetch", lambda url, **kw: real(url, resolver=resolver, **kw))
    assert notify._ntfy("a", "b", False, False) is False
    assert resolver.lookups == []


def test_a_bad_config_file_raises_rather_than_being_ignored(home):
    from halkit.config import ConfigError

    home.config.write_text("[notify]\nbogus = 1\n")
    with pytest.raises(ConfigError):
        notify.settings()

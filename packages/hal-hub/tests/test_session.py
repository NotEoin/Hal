"""Tests for `halhub.session`: taint, human messages, grants and files (REQ-HUB-21, REQ-HUB-68).

Taint rules are 01-security REQ-SEC-32 and REQ-SEC-93 as the hub applies them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from hubkit import ScriptedApprover, SpyRunner, answer

from halhub.audit import tail
from halhub.policy import Origin, Scope, Taint
from halhub.session import active_workspaces, parse_origin


def open_session(hub, kind="chat", **kwargs):
    kwargs.setdefault("origin", "cli")
    return hub.open_session(kind=kind, **kwargs)


@pytest.mark.parametrize("kind", ["hub", "code", "box", "remote"])
def test_hub_session_starts_untrusted(make_hub, kind):
    channel = "remote" if kind == "remote" else None
    assert open_session(make_hub(), kind, channel=channel).current_taint() is Taint.UNTRUSTED


def test_chat_session_starts_clean(make_hub):
    assert open_session(make_hub()).current_taint() is Taint.CLEAN


def test_resumed_chat_session_starts_untrusted_until_the_human_speaks(make_hub):
    s = open_session(make_hub(), resumed=True)
    assert s.current_taint() is Taint.UNTRUSTED
    s.human_message("carry on", surface="tui")
    assert s.current_taint() is Taint.CLEAN


def test_untrusted_result_raises_taint(make_hub, hal_home):
    hal_home.policy.write_text('schema_version = 1\n[egress]\ndomains = ["example.org"]\n')
    hub = make_hub(runner=SpyRunner(result="page"))
    s = open_session(hub)
    hub.call("demo_fetch", {"url": "https://example.org/"}, s, front_end="python")
    assert s.current_taint() is Taint.UNTRUSTED
    change = next(r for r in tail(n=0) if r["event"] == "taint_change")
    assert (change["from"], change["to"], change["cause"]) == ("clean", "untrusted", "untrusted_output")


def test_human_message_resets_taint(make_hub):
    hub = make_hub()
    chat, box = open_session(hub), open_session(hub, "box")
    for s in (chat, box):
        s.raise_taint(Taint.FLAGGED, cause="guard")
        s.human_message("turn the lights off", surface="tui")
    assert chat.current_taint() is Taint.CLEAN and box.current_taint() is Taint.UNTRUSTED
    assert chat.last_human_message == "turn the lights off"


def test_human_message_is_audited_as_length_and_digest(make_hub):
    s = open_session(make_hub())
    s.raise_taint(Taint.UNTRUSTED, cause="untrusted_output")
    s.human_message("private words", surface="tui")
    change = [r for r in tail(n=0) if r["event"] == "taint_change"][-1]
    assert change["message"]["chars"] == len("private words") and "private" not in json.dumps(change)


def test_voice_message_does_not_reset_flagged(make_hub):
    s = open_session(make_hub())
    s.raise_taint(Taint.FLAGGED, cause="guard")
    s.human_message("allow it", surface="voice")
    assert s.current_taint() is Taint.FLAGGED


@pytest.mark.parametrize("surface", ["headless", "routine"])
def test_headless_and_routine_text_is_kept_without_a_reset(make_hub, surface):
    s = open_session(make_hub())
    s.raise_taint(Taint.UNTRUSTED, cause="untrusted_output")
    s.human_message("run the briefing", surface=surface)
    assert s.current_taint() is Taint.UNTRUSTED and s.last_human_message == "run the briefing"


def test_taint_never_drops_by_raising(make_hub):
    s = open_session(make_hub())
    s.raise_taint(Taint.FLAGGED, cause="guard")
    s.raise_taint(Taint.UNTRUSTED, cause="untrusted_output")
    assert s.current_taint() is Taint.FLAGGED


def _files(hal_home) -> set[Path]:
    roots = (hal_home.config.parent, hal_home.state, hal_home.data, hal_home.runtime)
    audit = hal_home.state / "audit"
    return {p for root in roots for p in root.rglob("*") if p.is_file() and audit not in p.parents}


def test_pending_request_writes_no_files(make_hub, hal_home):
    seen = {}

    def look(request):
        seen["during"] = _files(hal_home)
        return answer("deny")(request)

    hub = make_hub()
    s = open_session(hub, approver=ScriptedApprover(look))
    before = _files(hal_home)
    hub.call("demo_wipe", {"path": "~/Downloads/a"}, s, front_end="python")
    assert seen["during"] == before and _files(hal_home) == before


def test_session_close_drops_session_grants(make_hub):
    approver = ScriptedApprover(answer("allow", Scope.SESSION))
    hub = make_hub()
    s = open_session(hub, approver=approver)
    hub.call("demo_wipe", {"path": "~/Downloads/a"}, s, front_end="python")
    assert s.grants.snapshot(s.id)
    s.close()
    assert s.grants.snapshot(s.id) == ()
    hub.call("demo_wipe", {"path": "~/Downloads/a"}, s, front_end="python")
    assert len(approver.requests) == 2


def test_context_carries_taint_grants_and_approver(make_hub):
    from halkit.testing import FakeClock

    s = open_session(make_hub(), approver=ScriptedApprover())
    ctx = s.context(FakeClock().now(), {})
    assert ctx.taint is Taint.CLEAN and ctx.grants == () and ctx.approver_available
    assert open_session(make_hub()).context(FakeClock().now(), {}).approver_available is False


def test_session_id_must_be_12_hex(make_hub):
    with pytest.raises(ValueError, match="12 lowercase hex"):
        open_session(make_hub(), session_id="XYZ")


def test_remote_origin_carries_the_client_and_needs_the_remote_channel(make_hub):
    assert parse_origin("remote:laptop") == (Origin.REMOTE, "laptop")
    with pytest.raises(ValueError, match="remote channel"):
        open_session(make_hub(), "remote", origin="remote:laptop")
    s = open_session(make_hub(), "remote", origin="remote:laptop", channel="remote")
    assert s.fields()["origin"] == "remote:laptop"


def test_unknown_origin_is_refused():
    with pytest.raises(ValueError, match="unknown origin"):
        parse_origin("model")


def test_active_workspaces_lists_live_session_records_only(hal_home, tmp_path):
    stat = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = stat.rsplit(")", 1)[1].split()[19]
    for sid, pid, pidstart, work in (
        ("aaaaaaaaaaaa", os.getpid(), start, tmp_path / "live"),
        ("bbbbbbbbbbbb", os.getpid(), "1", tmp_path / "stale"),
    ):
        directory = hal_home.runtime / "s" / sid
        directory.mkdir(parents=True)
        (directory / "owner.json").write_text(
            json.dumps({"pid": pid, "pidstart": pidstart, "work": str(work)})
        )
    assert active_workspaces() == ((tmp_path / "live").resolve(),)

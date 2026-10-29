"""Tests for the pipeline's audit records through the session's `EventSink` (REQ-HUB-50, REQ-HUB-51).

One case per front end reaches the one audit log; a broker session's own sink replaces it. The
records are 01-security's schema ("Audit log") through `halhub.audit.AuditLog`.
"""

from __future__ import annotations

import textwrap

from hubkit import SpyRunner

from halhub.audit import AuditLog, AuditUnavailable, default_directory, tail


class ListSink:
    """An `EventSink` that keeps records in memory; `fail_on` names events that raise."""

    def __init__(self, fail_on: frozenset[str] = frozenset()) -> None:
        self.records: list[dict] = []
        self.fail_on = fail_on

    def append(self, event: str, **fields: object) -> None:
        if event in self.fail_on:
            raise AuditUnavailable(f"cannot write {event}")
        self.records.append({"event": event, **fields})


def _events(records) -> list[str]:
    return [r["event"] for r in records]


def test_cli_call_writes_decision_and_result(make_hub):
    hub = make_hub()
    s = hub.open_session(kind="hub", origin="cli")
    r = hub.call("demo_echo", {"text": "hi"}, s, front_end="cli")
    records = list(tail(n=0))
    assert _events(records) == ["decision", "result"]
    decision, result = records
    assert (decision["session"], decision["origin"], decision["profile"]) == (s.id, "cli", "default")
    assert decision["call"] == result["call"] == r.call_id
    assert (decision["verdict"], decision["rule"], decision["taint"]) == ("allow", "tier:read", "untrusted")
    assert decision["key"].startswith("sha256:") and decision["args"] == {"text": "hi"}
    assert result["ok"] is True and result["chars"] == len("ok") and "content" not in result


def test_mcp_call_writes_decision_and_result(make_hub):
    hub = make_hub()
    s = hub.open_session(kind="hub", origin="host_mcp")
    hub.call("demo_echo", {"text": "hi"}, s, front_end="mcp")
    records = list(tail(n=0))
    assert _events(records) == ["decision", "result"] and records[0]["origin"] == "host_mcp"


def test_broker_session_uses_its_own_sink(make_hub):
    hub = make_hub()
    sink = ListSink()
    s = hub.open_session(kind="box", origin="box_agent", audit=sink)
    hub.call("demo_echo", {"text": "hi"}, s, front_end="broker")
    assert _events(sink.records) == ["decision", "result"]
    assert not default_directory().exists()


def test_unwritable_audit_refuses_call(make_hub, tmp_path):
    blocked = tmp_path / "audit"
    blocked.write_text("a file where the audit directory should be")
    spy = SpyRunner()
    hub = make_hub(runner=spy)
    s = hub.open_session(kind="hub", origin="cli", audit=AuditLog(blocked))
    r = hub.call("demo_echo", {"text": "hi"}, s, front_end="cli")
    assert (r.status, r.error_type) == ("error", "audit_unavailable") and spy.calls == []


def test_failed_result_record_still_returns_result(make_hub):
    hub = make_hub(runner=SpyRunner(result="done"))
    sink = ListSink(fail_on=frozenset({"result"}))
    s = hub.open_session(kind="hub", origin="cli", audit=sink)
    r = hub.call("demo_echo", {"text": "hi"}, s, front_end="cli")
    assert r.status == "ok" and r.result == "done" and _events(sink.records) == ["decision"]


def test_failed_approval_request_record_refuses_the_call(make_hub):
    from hubkit import ScriptedApprover

    spy, approver = SpyRunner(), ScriptedApprover()
    hub = make_hub(runner=spy)
    s = hub.open_session(
        kind="hub", origin="cli", approver=approver, audit=ListSink(frozenset({"approval_request"}))
    )
    r = hub.call("demo_wipe", {"path": "~/Downloads/a"}, s, front_end="cli")
    assert r.error_type == "audit_unavailable" and approver.requests == [] and spy.calls == []


def test_no_hub_calls_log_is_written(make_hub, hal_home):
    hub = make_hub()
    s = hub.open_session(kind="hub", origin="cli")
    hub.call("demo_echo", {"text": "hi"}, s, front_end="cli")
    hub.call("nope_tool", {}, s, front_end="cli")
    written = [p.name for root in (hal_home.state, hal_home.data) for p in root.rglob("*") if p.is_file()]
    assert "hub-calls.jsonl" not in written
    assert all(p.parent == default_directory() for p in hal_home.state.rglob("*.jsonl"))


def test_content_parameter_is_summarised_in_the_decision_record(make_hub, tmp_path):
    pack = tmp_path / "notes"
    pack.mkdir()
    (pack / "hal-module.toml").write_text(
        textwrap.dedent(
            """
            schema = 2
            [module]
            name = "notes"
            summary = "s"
            python = "notes.tools"
            [[tools]]
            name = "notes_add"
            description = "Add a note."
            tier = "write"
            [tools.params.body]
            required = true
            content = true
            """
        )
    )
    hub = make_hub(paths=(pack,))
    s = hub.open_session(kind="hub", origin="cli")
    hub.call("notes_add", {"body": "a private note"}, s, front_end="cli")
    decision = next(r for r in tail(n=0) if r["event"] == "decision")
    assert decision["args"]["body"]["chars"] == len("a private note")
    assert "private" not in str(decision)

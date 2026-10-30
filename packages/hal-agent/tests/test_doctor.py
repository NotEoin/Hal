"""Tests for `hal-agent doctor` and the `sessions` subcommands."""

import json

from halagent import cli
from halagent.doctor import FAIL, OK, WARN, run_checks
from halkit.testing.broker import FakeBroker
from halkit.testing.model_server import MODEL_ID, Reply


def by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_reports_a_healthy_setup(server, tmp_path):
    with FakeBroker(tmp_path / "b.sock") as broker:
        broker.add_tool("web_read", "x")
        checks = by_name(
            run_checks(None, {"endpoint": server.url, "model": MODEL_ID, "broker": str(broker.socket_path)})
        )
    assert (checks["gateway"].status, checks["broker"].status, checks["profile"].status) == (OK, OK, OK)
    assert checks["config"].status == OK and "1 tools" in checks["broker"].detail


def test_doctor_fails_when_the_gateway_is_unreachable(tmp_path):
    checks = by_name(run_checks(None, {"endpoint": "http://127.0.0.1:9"}))
    assert checks["gateway"].status == FAIL


def test_doctor_warns_when_the_model_is_not_listed_and_no_broker(server):
    checks = by_name(run_checks(None, {"endpoint": server.url, "model": "other"}))
    assert (checks["gateway"].status, checks["broker"].status) == (WARN, WARN)


def test_doctor_names_a_bad_run_file(tmp_path):
    bad = tmp_path / "agent.json"
    bad.write_text("{}")
    checks = by_name(run_checks(bad))
    assert checks["config"].status == FAIL and "agent.json" in checks["config"].detail


def test_doctor_command_exits_nonzero_on_a_failed_check(capsys):
    code = cli.main(["--endpoint", "http://127.0.0.1:9", "doctor", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert code == 1 and {r["name"]: r["status"] for r in rows}["gateway"] == "fail"


def test_sessions_show_and_export_to_a_file(server, tmp_path, capsys):
    server.script(Reply(tool_calls=[("finish", {"summary": "done it"})]))
    base = ["-C", str(tmp_path), "--endpoint", server.url]
    assert cli.main(["-p", "task", *base]) == 0
    capsys.readouterr()
    cli.main(["sessions", "list", "--cwd", str(tmp_path)])
    sid = capsys.readouterr().out.split()[0]
    assert cli.main(["sessions", "show", sid]) == 0
    shown = capsys.readouterr().out
    assert "turns: 1" in shown and "finished" in shown and "done it" in shown
    out = tmp_path / "out.md"
    assert cli.main(["sessions", "export", sid, "--out", str(out)]) == 0
    assert "task" in out.read_text()


def test_sessions_show_needs_an_id(capsys):
    assert cli.main(["sessions", "show"]) == 2
    assert "needs a session id" in capsys.readouterr().err

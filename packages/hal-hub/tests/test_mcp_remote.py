"""Tests for `hal-hub mcp --channel remote` (REQ-HUB-71; 07 REQ-RMT-25; RESOLUTIONS B4, B8, F5).

The server runs in-process with the SDK client, as in `test_mcp.py`; the CLI cases replace
`serve_stdio` with a recorder, so they check the options without serving.
"""

from __future__ import annotations

import json
import threading
import time

import anyio
import mcp_types as types
import pytest
from mcp import Client

from halhub import _control_standin as control
from halhub import cli, mcp
from halhub._audit_standin import tail
from halhub.mcp import ServeOptions, build_server

REMOTE = ServeOptions(channel="remote", origin="remote:laptop", remote_addr="203.0.113.5")  # TEST-NET-3
APPROVE_WAIT_S = 10


def call(hub, mode, name, args, options=REMOTE, elicit=None):
    async def main():
        async with Client(build_server(hub, options), mode=mode, elicitation_callback=elicit) as client:
            return await client.call_tool(name, args)

    return anyio.run(main)


async def accept_once(context, params):
    return types.ElicitResult(action="accept", content={"decision": "once"})


def test_remote_channel_uses_auto_mode_without_notify(make_hub):
    hub = make_hub()
    result = call(hub, "legacy", "demo_wipe", {"path": "/tmp/x"}, elicit=accept_once)
    assert result.content[0].text == "ok"
    requests = [r for r in tail(n=0) if r["event"] == "approval_request"]
    assert requests[0]["surfaces"] == ["elicitation", "control"]
    decisions = [r for r in tail(n=0) if r["event"] == "decision"]
    assert decisions[0]["origin"] == "remote:laptop" and decisions[0]["channel"] == "remote"


def test_remote_client_without_elicitation_waits_for_hal_approve(make_hub, monkeypatch):
    monkeypatch.setattr(control, "peer_in_harness", lambda pid: False)
    hub = make_hub()

    def approve() -> None:
        deadline = time.monotonic() + APPROVE_WAIT_S
        while time.monotonic() < deadline:
            pending = control.list_pending()
            if pending:
                control.answer(pending[0], "allow", scope="once")
                return
            time.sleep(0.05)

    thread = threading.Thread(target=approve, daemon=True)
    thread.start()
    result = call(hub, "2026-07-28", "demo_wipe", {"path": "/tmp/x"})
    thread.join(5)
    assert result.content[0].text == "ok"
    approvals = [r for r in tail(n=0) if r["event"] == "approval"]
    assert [(r["verdict"], r["source"]) for r in approvals] == [("allow", "control")]


def test_remote_channel_profile_denies_the_desktop_pack(make_hub, tmp_path):
    # a path-scanned module's pack is its module name, so this module is pack `desktop`
    root = tmp_path / "desktop"
    (root / "haldesk").mkdir(parents=True)
    (root / "haldesk" / "__init__.py").write_text("")
    (root / "haldesk" / "tools.py").write_text("def shot() -> str:\n    return 'png'\n")
    (root / "hal-module.toml").write_text(
        'schema = 2\n[module]\nname = "desktop"\nsummary = "Desktop"\npython = "haldesk.tools"\n'
        '[[tools]]\nname = "desktop_shot"\nfunction = "shot"\ndescription = "Take a screenshot."\n'
        'tier = "read"\n'
    )
    hub = make_hub("demo", paths=(root,))
    data = json.loads(call(hub, "legacy", "desktop_shot", {}).content[0].text)
    assert data["status"] == "denied" and hub.runner.calls == []
    local = call(hub, "legacy", "desktop_shot", {}, options=ServeOptions())  # the same tool, on the host
    assert local.content[0].text == "ok" and not local.is_error


def test_remote_addr_is_in_every_audit_record(make_hub):
    hub = make_hub()
    call(hub, "legacy", "hal_call", {"tool": "demo_echo", "arguments": {"text": "hi"}})
    call(hub, "legacy", "demo_wipe", {"path": "/x"}, elicit=accept_once)
    records = list(tail(n=0))
    assert len(records) >= 4
    assert all(r["remote_addr"] == "203.0.113.5" for r in records)


def test_origin_requires_remote_channel():
    with pytest.raises(ValueError, match="channel remote"):
        ServeOptions(origin="remote:laptop")
    with pytest.raises(ValueError, match="remote:<client>"):
        ServeOptions(channel="remote", origin="remote:Laptop!")
    with pytest.raises(ValueError, match="auto"):
        ServeOptions(channel="remote", approvals="out-of-band")
    with pytest.raises(ValueError, match="channel remote"):
        ServeOptions(remote_addr="203.0.113.5")


# ---- the CLI ----------------------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    """Replaces `serve_stdio` with a recorder of the options it was given."""
    seen: list[ServeOptions] = []
    monkeypatch.setattr(mcp, "serve_stdio", lambda hub, options: seen.append(options))
    return seen


def test_cli_remote_channel_reads_the_remote_address(served, monkeypatch):
    monkeypatch.setenv("HAL_REMOTE_ADDR", "203.0.113.5")
    assert cli.main(["mcp", "--channel", "remote", "--origin", "remote:laptop"]) == 0
    options = served[0]
    assert (options.channel, options.origin, options.remote_addr) == (
        "remote",
        "remote:laptop",
        "203.0.113.5",
    )
    assert options.approvals == "auto"


def test_cli_ignores_the_remote_address_without_the_remote_channel(served, monkeypatch):
    monkeypatch.setenv("HAL_REMOTE_ADDR", "203.0.113.5")
    assert cli.main(["mcp"]) == 0
    assert served[0].remote_addr is None and served[0].origin == "host_mcp"


@pytest.mark.parametrize(
    "argv",
    [
        ["mcp", "--origin", "remote:laptop"],
        ["mcp", "--channel", "remote", "--origin", "laptop"],
        ["mcp", "--channel", "remote", "--origin", "remote:" + "a" * 33],
        ["mcp", "--channel", "remote", "--approvals", "out-of-band"],
        ["mcp", "--channel", "voice"],
    ],
)
def test_cli_rejects_bad_remote_options_with_exit_2(served, argv, capsys):
    assert cli.main(argv) == 2 and served == []
    assert "hal-hub" in capsys.readouterr().err

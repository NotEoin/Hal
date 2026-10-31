"""`BoxedAgent.start` hands the agent.json run file to the sandbox session (REQ-CLI-99)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from halcore import sessions


class _Stop(Exception):
    pass


def test_box_start_passes_agent_json_run_file(monkeypatch, tmp_path: Path):
    seen: dict = {}

    def fake_start(project, **kw):
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(sessions.Session, "start", staticmethod(fake_start))
    with pytest.raises(_Stop):
        asyncio.run(sessions.BoxedAgent.start(kind="code", project=tmp_path, channel="cli", model="m1"))
    files = seen["run_files"]("0123456789ab")
    data = json.loads(files["agent.json"])
    assert "m1" in json.dumps(data)
    assert ["--config", sessions.AGENT_CONFIG] == seen["child_argv"][seen["child_argv"].index("--config") :][:2]

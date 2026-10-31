"""`hal acp`: the ACP proxy between an editor or channel on stdio and the boxed agent.

    hal acp [DIR] [--direct] [--model M] [--upstream URL] [--image I] [--runtime runc|runsc]
    hal acp --channel NAME --toolset assistant

Voice, Telegram and editors use this command and never import `halcore` (REQ-CLI-41). stdout
carries only protocol; logs go to stderr, which editors capture. The agent runs in a box
(RESOLUTIONS I1); broker approvals reach the client as host-originated permission requests.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from halsandbox.errors import SandboxError

from . import sessions
from . import pids
from .acp.hostfs import CopyFs
from .acp.permissions import PermissionBridge, socket_owned_by_us
from .acp.proxy import Proxy, ProxyFilter
from .acp.records import RecordStore
from .acp.transport import AgentCrashed, stdio_transport

ZED_CONFIG = '{"agent_servers": {"Hal": {"command": "hal", "args": ["acp"]}}}'
EXIT_FAILED, EXIT_REFUSED = 1, 3  # 06-core-cli exit code table: 3 = refused to start


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hal acp",
        description="Serve the Agent Client Protocol on stdio, backed by a boxed Hal agent.",
        epilog=f"Zed configuration:\n  {ZED_CONFIG}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("dir", nargs="?", default=".", help="project directory (coding toolset)")
    p.add_argument("--channel", help="channel name for an assistant box (voice, telegram, ...)")
    p.add_argument("--toolset", choices=("coding", "assistant"), default="coding")
    p.add_argument("--direct", action="store_true", help="mount DIR itself (lockdown relaxed or lower)")
    p.add_argument("--model")
    p.add_argument("--upstream")
    p.add_argument("--image", default=sessions.sandbox.DEFAULT_IMAGE)
    p.add_argument("--runtime", choices=("runc", "runsc"))
    return p


async def serve(opts: argparse.Namespace) -> None:
    """Starts the box and pumps the proxy until the editor or the agent hangs up."""
    assistant = opts.toolset == "assistant"
    if assistant and not opts.channel:
        raise SandboxError("--toolset assistant needs --channel NAME")
    project = None if assistant else Path(opts.dir).resolve()
    box = await sessions.BoxedAgent.start(
        kind="chat" if assistant else "code",
        project=project,
        channel=opts.channel or "acp",
        upstream=opts.upstream,
        model=opts.model,
        image=opts.image,
        runtime=opts.runtime,
        direct=opts.direct,
    )
    try:
        rules = ProxyFilter(
            project=project,
            scratch=Path(sessions.BOX_SCRATCH) if assistant else None,
            box_root=None if assistant else sessions.BOX_WORK,
            on_human_message=lambda text: box.session.broker.on_human_message(text, sessions.SURFACE_ACP),
            records=RecordStore(),
            copy=CopyFs(box.session.iso.work, project, sessions.BOX_WORK) if box.session.iso and project else None,
        )
        bridge = PermissionBridge(socket_owned_by_us, lambda: rules.active)
        rules.own_response = bridge.consume  # the filter and the bridge need each other
        await Proxy(await stdio_transport(), box.transport, rules, bridge).run()
    finally:
        await box.close()
        if box.session.iso is not None:
            logging.getLogger(__name__).warning(
                "changes are in isolated session %s; `hal code accept %s` applies them",
                box.session.iso.sid,
                box.session.iso.sid,
            )


def main(argv: list[str]) -> int:
    """Runs `hal acp`; returns the exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    opts = _parser().parse_args(argv)
    try:
        with pids.registered("acp"):
            asyncio.run(serve(opts))
    except SandboxError as exc:
        print(f"hal acp: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AgentCrashed as exc:
        print(f"hal acp: the agent failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return 0

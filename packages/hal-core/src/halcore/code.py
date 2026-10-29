"""`hal code [DIR]`: the secure coding path, hal-agent in a docker box (see halsandbox.code).

    hal code [DIR] [--upstream URL] [--model NAME] [--image NAME] [--runtime runc|runsc]
             [-- AGENT ARGS...]
    hal code --build

Without `--build` it needs the agent image; `--build` creates it from this checkout. The model
server and model default to Hal's own `[hal] ollama_url` and `chat_model`, so `hal code` uses
the same local model as `hal chat`; HAL_CODE_UPSTREAM and HAL_AGENT_MODEL override them.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from halsandbox import code as sandbox

from . import config

ROOT = Path(__file__).resolve().parents[4]  # packages/hal-core/src/halcore -> checkout root


def main(argv: list[str]) -> int:
    """Runs `hal code`; returns the exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--" in argv:
        cut = argv.index("--")
        argv, agent_args = argv[:cut], argv[cut + 1 :]
    else:
        agent_args = []
    p = argparse.ArgumentParser(prog="hal code", description="run hal-agent in a docker box")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--build", action="store_true", help="build the agent image and exit")
    shared = config.shared()
    p.add_argument("--upstream", default=os.environ.get("HAL_CODE_UPSTREAM", shared["ollama_url"]))
    p.add_argument("--model", default=os.environ.get("HAL_AGENT_MODEL", shared["chat_model"]))
    p.add_argument("--image", default=sandbox.DEFAULT_IMAGE)
    p.add_argument("--runtime", choices=("runc", "runsc"))
    opts = p.parse_args(argv)
    if "--model" not in agent_args:
        agent_args = ["--model", opts.model, *agent_args]
    try:
        if opts.build:
            sandbox.build_image(ROOT, opts.image)
            return 0
        return sandbox.run_code(
            Path(opts.dir), upstream=opts.upstream, image=opts.image, runtime=opts.runtime, agent_args=agent_args
        )
    except (sandbox.SandboxError, ValueError, OSError) as exc:
        print(f"hal code: {exc}", file=sys.stderr)
        return 1

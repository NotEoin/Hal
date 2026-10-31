"""`hal ide setup EDITOR [--print | --write PATH] [--force]`: configuration snippets for editors.

The command never guesses where an editor keeps its settings and never edits a real config on
its own: `--print` (the default) writes the snippet to stdout and `--write PATH` writes only the
file the user names. An existing file is refused without `--force`, and with `--force` it is
copied to `PATH.bak` first. The snippets are whole files, not merges: a merge would need a
parser for each editor's config dialect (Zed settings are JSONC, for example).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

EXIT_USAGE, EXIT_REFUSED = 2, 3  # 06-core-cli exit codes: 3 = refused
ACP_SERVER = {"command": "hal", "args": ["acp"]}
BACKUP_SUFFIX = ".bak"

NEOVIM = """\
-- CodeCompanion: add to the setup() table
adapters = {
  acp = {
    hal = function()
      return require("codecompanion.adapters").extend("gemini_cli", {
        name = "hal",
        formatted_name = "Hal",
        commands = { default = { "hal", "acp" } },
      })
    end,
  },
},
strategies = { chat = { adapter = "hal" } },

-- avante.nvim: add to the setup() table
acp_providers = {
  ["hal"] = { command = "hal", args = { "acp" } },
},
provider = "hal",
"""

EMACS = """\
;; agent-shell: register Hal as an ACP agent (check your agent-shell version for the
;; exact name of the config helper; the command line is what matters)
(setq agent-shell-hal-command '("hal" "acp"))
(add-to-list 'agent-shell-agent-configs
             (agent-shell-make-agent-config
              :identifier 'hal
              :mode-line-name "Hal"
              :client-maker (lambda (buffer)
                              (agent-shell--make-acp-client :command "hal"
                                                            :command-params '("acp")
                                                            :context-buffer buffer))))
"""


def _json(data: object) -> str:
    return json.dumps(data, indent=2) + "\n"


SNIPPETS = {
    "zed": _json({"agent_servers": {"Hal": ACP_SERVER}}),
    "jetbrains": _json({"agent_servers": {"Hal": ACP_SERVER}}),
    "neovim": NEOVIM,
    "emacs": EMACS,
    "vscode": _json({"hal.command": "hal", "hal.shareActiveFile": False}),
}
HINTS = {
    "zed": "Merge into Zed's settings.json (command palette: zed: open settings).",
    "jetbrains": "Save as ~/.jetbrains/acp.json, or use AI Assistant > Add Custom Agent.",
    "neovim": "Put the block for your plugin into its setup() call.",
    "emacs": "Put this in your init file after agent-shell loads.",
    "vscode": "Merge into settings.json, then build the extension in ide/vscode (see the how-to).",
}


class IdeError(Exception):
    """A request the command refuses."""


def write_snippet(editor: str, target: Path, *, force: bool = False) -> Path | None:
    """Writes the snippet to `target`; returns the backup path when one was made.

    Raises `IdeError` if `target` exists and `force` is false, or is not a regular file.
    """
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise IdeError(f"{target} is not a regular file")
    backup = None
    if target.exists():
        if not force:
            raise IdeError(f"{target} exists; pass --force to replace it (a {BACKUP_SUFFIX} copy is kept)")
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".hal-ide-")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(SNIPPETS[editor])
    os.replace(tmp, target)
    return backup


def main(argv: list[str]) -> int:
    """Runs `hal ide`; returns the exit status."""
    p = argparse.ArgumentParser(prog="hal ide", description="Connect an editor to Hal.")
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("setup", help="print or write an editor configuration")
    s.add_argument("editor", choices=sorted(SNIPPETS))
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="show", action="store_true", help="print the snippet (default)")
    mode.add_argument("--write", metavar="PATH", help="write the snippet to this file only")
    s.add_argument("--force", action="store_true", help="replace an existing file (keeps a .bak)")
    opts = p.parse_args(argv)
    if opts.write is None:
        print(SNIPPETS[opts.editor], end="")
        print(f"# {HINTS[opts.editor]}", file=sys.stderr)
        return 0
    try:
        backup = write_snippet(opts.editor, Path(opts.write).expanduser(), force=opts.force)
    except IdeError as exc:
        print(f"hal ide: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"wrote {opts.write}" + (f" (old file kept as {backup})" if backup else ""))
    print(HINTS[opts.editor])
    return 0

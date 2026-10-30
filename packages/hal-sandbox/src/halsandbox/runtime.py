"""The per-session runtime directory under `$XDG_RUNTIME_DIR`: sockets, `/etc` files, `owner.json`.

Lives under the runtime dir, not the state dir, because a unix socket path is limited to 107
bytes (02-sandbox D2). The directory is 0700 and the box sees only `run/`, bound read-only at
`/run/hal` (D1), so it can connect to a socket but never replace one.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from halkit import paths

from .errors import SandboxError

SOCKETS = ("broker.sock", "model.sock", "egress.sock", "lifeline.sock")
OWNER_KEYS = ("pid", "pidstart", "uid", "kind", "backend", "container", "work", "created")
SESSION_SCHEMA = 1
BOX_USER = "hal"
BOX_HOME = "/home/hal"
BOX_SHELL = "/bin/bash"
NOGROUP_GID = 65534  # the conventional "nogroup" id, used only when the host gid is unusable


def new_session_id() -> str:
    """A fresh session id: 12 lowercase hex characters."""
    return secrets.token_hex(6)


def utc_now() -> str:
    """The current time as UTC ISO 8601 with a `Z`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pidstart(pid: int) -> int:
    """Field 22 of `/proc/<pid>/stat` (start time in ticks); 0 when it cannot be read."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0
    # the command name is in parentheses and may hold spaces, so split after the last ")"
    return int(text.rpartition(")")[2].split()[19])


def passwd_text(uid: int, gid: int) -> str:
    """`/etc/passwd` naming `hal` with the host uid, plus root and nobody."""
    return (
        "root:x:0:0:root:/root:/usr/sbin/nologin\n"
        f"{BOX_USER}:x:{uid}:{gid}:Hal box user:{BOX_HOME}:{BOX_SHELL}\n"
        f"nobody:x:{NOGROUP_GID}:{NOGROUP_GID}:nobody:/nonexistent:/usr/sbin/nologin\n"
    )


def group_text(gid: int) -> str:
    """`/etc/group` naming the host gid `hal`, plus root and nogroup."""
    return f"root:x:0:\n{BOX_USER}:x:{gid}:\nnogroup:x:{NOGROUP_GID}:\n"


class RuntimeDir:
    """`$R/s/<sid>/` with its generated files; create it, write files, remove it."""

    def __init__(self, sid: str, root: Path) -> None:
        self.sid = sid
        self.root = root

    @property
    def run(self) -> Path:
        """The directory bound at `/run/hal`."""
        return self.root / "run"

    @property
    def etc(self) -> Path:
        """The directory holding the generated `/etc` files."""
        return self.root / "etc"

    @property
    def empty(self) -> Path:
        """The zero-byte file bound over each masked file."""
        return self.root / "empty"

    def socket(self, name: str) -> Path:
        """The path of one socket in `run/`; raises unless `name` is a known socket."""
        if name not in SOCKETS:
            raise ValueError(f"unknown socket {name!r}: expected one of {', '.join(SOCKETS)}")
        return paths.check_socket_path(self.run / name)

    @classmethod
    def create(cls, sid: str | None = None, *, uid: int | None = None, gid: int | None = None) -> RuntimeDir:
        """Creates the directory tree, `empty`, `etc/passwd` and `etc/group`.

        Raises `SocketPathTooLong` before anything is written when a socket would not fit, and
        `SandboxError` when the directory already exists (a session id is never reused).
        """
        sid = sid or new_session_id()
        root = paths.session_dir(sid)
        for name in SOCKETS:  # fail first: nothing exists yet to clean up
            paths.check_socket_path(root / "run" / name)
        if root.exists():
            raise SandboxError(f"session directory {root} already exists")
        paths.ensure_private_dir(root.parent)
        paths.ensure_private_dir(root)
        rd = cls(sid, root)
        paths.ensure_private_dir(rd.run)
        rd.etc.mkdir(mode=0o755)
        os.chmod(rd.etc, 0o755)  # mkdir honours the umask; the box needs to traverse it
        uid = os.getuid() if uid is None else uid
        gid = os.getgid() if gid is None else gid
        _write(rd.etc / "passwd", passwd_text(uid, gid), 0o644)
        _write(rd.etc / "group", group_text(gid), 0o644)
        _write(rd.empty, "", 0o444)
        return rd

    def write_owner(self, *, kind: str, backend: str, container: str, work: str) -> dict[str, object]:
        """Writes `owner.json` (every key present) for the sweep and the active-workspace rule."""
        pid = os.getpid()
        data: dict[str, object] = {
            "pid": pid,
            "pidstart": pidstart(pid),
            "uid": os.getuid(),
            "kind": kind,
            "backend": backend,
            "container": container,
            "work": work,
            "created": utc_now(),
        }
        _write(self.root / "owner.json", json.dumps(data, indent=2), 0o600)
        return data

    def write_session_json(self, sandbox: Mapping[str, object], **fields: object) -> dict[str, object]:
        """Writes `run/session.json` (0644, no secrets) around the `sandbox` capabilities object."""
        data: dict[str, object] = {
            "schema": SESSION_SCHEMA,
            "session_id": self.sid,
            "kind": fields.pop("kind", "code"),
            "created": utc_now(),
            "sandbox": dict(sandbox),
            "sockets": {n[:-5]: f"/run/hal/{n}" for n in ("broker.sock", "model.sock", "lifeline.sock")},
            "relay": fields.pop("relay", {"enabled": False, "port": 8741}),
            "workdir": fields.pop("workdir", "/work"),
        }
        data.update(fields)
        _write(self.run / "session.json", json.dumps(data, indent=2), 0o644)
        return data

    def remove(self) -> None:
        """Deletes the whole directory; missing is fine."""
        shutil.rmtree(self.root, ignore_errors=True)


def read_cid(root: Path) -> str | None:
    """The container id docker wrote to `<root>/cid` (`--cidfile`), or None while it is missing."""
    try:
        text = (root / "cid").read_text().strip()
    except OSError:
        return None
    return text if re.fullmatch(r"[0-9a-f]{12,64}", text) else None


def read_owner(root: Path) -> dict[str, object]:
    """The parsed `owner.json` of a session directory; raises `SandboxError` when unusable."""
    try:
        data = json.loads((root / "owner.json").read_text())
    except (OSError, ValueError) as exc:
        raise SandboxError(f"no usable owner.json in {root}: {exc}") from exc
    missing = [k for k in OWNER_KEYS if k not in data]
    if missing:
        raise SandboxError(f"owner.json in {root} lacks {missing}")
    return data


def _write(path: Path, text: str, mode: int) -> None:
    """Creates `path` with `mode` set before any content is written, then replaces it atomically."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)

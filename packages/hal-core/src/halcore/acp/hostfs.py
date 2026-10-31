"""ACP `fs/*` served by the host from the isolated copy, and ACP diff content for edits.

The boxed agent may ask the client to read or write a text file. The proxy answers on the host
from the session's isolated copy (`iso.work`), never from the real repository, so an editor
user gets the usual ACP file semantics while 'nothing reaches the real repo before accept' (I3)
still holds. Paths are opened with `halkit.fsafe.open_regular_beneath`, so a symlink the agent planted in
the copy cannot send a read or write elsewhere. `terminal/*` is not served: the box already has
its own exec path, and a host terminal would be a way out.

`edit_diff` turns an `edit` or `write` tool call into the ACP `diff` content block, with the
editor's own (real project) path, so Zed and JetBrains render the change natively.
"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path, PurePosixPath

from halkit import fsafe

log = logging.getLogger(__name__)

MAX_READ_BYTES = 1 << 20  # a file a 64k-token model can take at most
MAX_WRITE_BYTES = 2 << 20
ERR_INVALID_PARAMS, ERR_INTERNAL = -32602, -32603
Message = dict[str, object]


class FsRefused(Exception):
    """A file request the host will not serve; carries the JSON-RPC error code."""

    def __init__(self, text: str, code: int = ERR_INVALID_PARAMS) -> None:
        super().__init__(text)
        self.code = code


class CopyFs:
    """Reads and writes text files below one isolated copy."""

    def __init__(self, work: Path, project: Path, box_root: str) -> None:
        self.work, self.project, self.box_root = work, project.resolve(), box_root

    def relative(self, raw: object) -> str:
        """The copy-relative path of an agent path (a box path or a project path)."""
        if not isinstance(raw, str) or not raw.startswith("/"):
            raise FsRefused("path must be absolute")
        pure = PurePosixPath(raw)
        if ".." in pure.parts or "\0" in raw:
            raise FsRefused("path must not contain '..'")
        for base in (PurePosixPath(self.box_root), PurePosixPath(self.project.as_posix())):
            if pure == base or base in pure.parents:
                rel = pure.relative_to(base).as_posix()
                if rel == ".":
                    break
                return rel
        raise FsRefused(f"path {raw} is outside the project")

    def read(self, params: Message) -> Message:
        """Answers `fs/read_text_file`, honouring the optional 1-based `line` and `limit`."""
        rel = self.relative(params.get("path"))
        try:
            fd = fsafe.open_regular_beneath(self.work, rel)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(MAX_READ_BYTES + 1)
        except (OSError, fsafe.UnsafePathError) as exc:
            raise FsRefused(f"cannot read {rel}: {exc}") from None
        if len(data) > MAX_READ_BYTES:
            raise FsRefused(f"{rel} is over the {MAX_READ_BYTES} byte read cap")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise FsRefused(f"{rel} is not UTF-8 text") from None
        line, limit = params.get("line"), params.get("limit")
        if isinstance(line, int) or isinstance(limit, int):
            lines = text.splitlines(keepends=True)
            start = max(int(line) - 1, 0) if isinstance(line, int) else 0
            end = start + int(limit) if isinstance(limit, int) else len(lines)
            text = "".join(lines[start:end])
        return {"content": text}

    def write(self, params: Message) -> Message:
        """Answers `fs/write_text_file` inside the copy; the parent folder must exist."""
        rel = self.relative(params.get("path"))
        content = params.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise FsRefused("content must be text under the write cap")
        try:
            fd = fsafe.open_regular_beneath(self.work, rel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, "wb") as fh:
                fh.write(content.encode("utf-8"))
        except (OSError, fsafe.UnsafePathError) as exc:
            code = ERR_INVALID_PARAMS if getattr(exc, "errno", None) in (errno.ENOENT, errno.ENOTDIR) else ERR_INTERNAL
            raise FsRefused(f"cannot write {rel}: {exc}", code) from None
        return {}

    def serve(self, method: str, params: Message) -> Message | None:
        """Result for `fs/read_text_file` or `fs/write_text_file`; None for any other method."""
        if method == "fs/read_text_file":
            return self.read(params)
        if method == "fs/write_text_file":
            return self.write(params)
        return None

    def edit_diff(self, tool_call: Message) -> list[Message]:
        """ACP `diff` content for an `edit` or `write` tool call, or an empty list."""
        raw = tool_call.get("rawInput")
        if tool_call.get("kind") != "edit" or not isinstance(raw, dict):
            return []
        try:
            rel = self.relative(raw["path"] if str(raw.get("path", "")).startswith("/") else f"{self.box_root}/{raw.get('path')}")
            if "content" in raw and isinstance(raw["content"], str):
                try:
                    old: str | None = self.read({"path": f"{self.box_root}/{rel}"})["content"]  # type: ignore[assignment]
                except FsRefused:
                    old = None
                new = raw["content"]
            elif isinstance(raw.get("old_string"), str) and isinstance(raw.get("new_string"), str):
                old, new = raw["old_string"], raw["new_string"]
            else:
                return []
        except (FsRefused, KeyError):
            return []
        return [{"type": "diff", "path": str(self.project / rel), "oldText": old, "newText": new}]

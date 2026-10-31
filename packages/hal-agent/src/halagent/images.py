"""`@image` references in a prompt: reads the files and returns them as `{mimeType, data}` images.

A token like `@shots/error.png` names a file under the working copy. Only real image files of a
known type and a sane size are attached; anything else stays plain text, so `@mention` words and
paths to other files are never read here. Paths that leave the workspace are refused, which keeps
a hostile prompt from reading `~/.ssh` through this route.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp"}  # fmt: skip
MAX_IMAGE_BYTES = 8_000_000  # a vision model downscales anyway; larger files only fill the request
MAX_IMAGES = 4
_TOKEN = re.compile(r"(?<!\S)@(\S+)")


class ImageError(ValueError):
    """A referenced image is unusable; the message says why."""


def extract_images(prompt: str, root: Path) -> list[dict]:
    """The images that `@path` tokens in `prompt` name, in order; raises `ImageError` on a bad one."""
    root = root.resolve()
    images: list[dict] = []
    for token in _TOKEN.findall(prompt):
        token = token.rstrip(".,;:)")
        suffix = Path(token).suffix.lower()
        if suffix not in MIME_TYPES:
            continue
        path = (root / token).resolve()
        if not path.is_relative_to(root):
            raise ImageError(f"@{token} is outside the working copy")
        if not path.is_file():
            continue  # not a file here: leave the text alone
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ImageError(f"@{token} is larger than {MAX_IMAGE_BYTES // 1_000_000} MB")
        if len(images) >= MAX_IMAGES:
            raise ImageError(f"at most {MAX_IMAGES} images per prompt")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        images.append({"mimeType": MIME_TYPES[suffix], "data": data})
    return images

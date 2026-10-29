"""A throwaway Hal home: every Hal path variable points into a temporary directory."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from types import TracebackType

ENV_NAMES = ("HAL_CONFIG", "HAL_POLICY", "HAL_STATE_DIR", "HAL_DATA_DIR", "HAL_RUNTIME_DIR")
DEPRECATED_NAMES = ("HAL_STATE", "HAL_DATA")  # cleared so an old alias cannot leak in
XDG_NAMES = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")


class TempHalHome:
    """Context manager that isolates a test from the real `~/.config/hal` and friends.

    Inside the block `halkit.paths` resolves everything under a new temporary directory, and
    the runtime directory (mode 0700) is short enough for unix sockets. On exit the
    environment is restored and the directory is deleted.
    """

    def __init__(self) -> None:
        self.root: Path
        self._saved: dict[str, str | None] = {}

    @property
    def config(self) -> Path:
        """The config file path (`HAL_CONFIG`); it does not exist until a test writes it."""
        return self.root / "config" / "config.toml"

    @property
    def policy(self) -> Path:
        """The policy file path (`HAL_POLICY`)."""
        return self.root / "config" / "policy.toml"

    @property
    def state(self) -> Path:
        """The state directory (`HAL_STATE_DIR`)."""
        return self.root / "state"

    @property
    def data(self) -> Path:
        """The data directory (`HAL_DATA_DIR`)."""
        return self.root / "data"

    @property
    def runtime(self) -> Path:
        """The runtime directory (`HAL_RUNTIME_DIR`), mode 0700."""
        return self.root / "run"

    def __enter__(self) -> TempHalHome:
        # a short prefix under the system temp dir keeps socket paths under 107 bytes
        self.root = Path(tempfile.mkdtemp(prefix="hal-"))
        for directory in (self.config.parent, self.state, self.data, self.runtime):
            directory.mkdir(mode=0o700, parents=True)
        values = {
            "HAL_CONFIG": str(self.config),
            "HAL_POLICY": str(self.policy),
            "HAL_STATE_DIR": str(self.state),
            "HAL_DATA_DIR": str(self.data),
            "HAL_RUNTIME_DIR": str(self.runtime),
        }
        names = ENV_NAMES + DEPRECATED_NAMES + XDG_NAMES
        self._saved = {name: os.environ.get(name) for name in names}
        for name in DEPRECATED_NAMES + XDG_NAMES:
            os.environ.pop(name, None)
        os.environ.update(values)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for name, old in self._saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        shutil.rmtree(self.root, ignore_errors=True)

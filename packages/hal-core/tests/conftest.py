"""Puts this folder on the import path so tests can share `halcore_env` under pytest.

`python -m unittest discover -s packages/hal-core/tests` already does this by itself.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


import pytest  # noqa: E402 - below the path setup that the tests above rely on


@pytest.fixture(autouse=True)
def no_real_gpu_tools(monkeypatch):
    """No test in this package may run `nvidia-smi`, `amd-smi` or `rocm-smi` on the real machine."""
    from halkit import hardware

    def refuse(argv, timeout):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(hardware, "default_runner", refuse)

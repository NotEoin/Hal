"""Puts this folder on the import path so tests can share `halcore_env` under pytest.

`python -m unittest discover -s packages/hal-core/tests` already does this by itself.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

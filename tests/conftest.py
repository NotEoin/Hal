"""Puts this folder on the import path so the cross-package tests can share `test_layering`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

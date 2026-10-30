"""`python -m halremote` runs the same entry point as the `hal-remote` script."""

import sys

from .cli import main

sys.exit(main())

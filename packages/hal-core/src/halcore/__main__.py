"""python3 -m halcore <command>: the Python half of the `hal` launcher; see `halcore.cli`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

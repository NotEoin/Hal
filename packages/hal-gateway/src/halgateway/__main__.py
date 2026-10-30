"""`python -m halgateway` runs the `hal-gateway` command line."""

import sys

from .cli import main

sys.exit(main())

"""The Hal sandbox: the broker that wires policy, approvals and audit from `halhub`, container
backends (docker runc or runsc, bubblewrap), images, `hal box`, dependency prep, the in-container
relay and MCP bridge, and workspace checkpoints.

It never imports `halagent` or `halgateway`. Design: docs/design/02-sandbox.md.
This package is a skeleton: the first behaviour lands in phase 5 of the implementation plan.
"""

__version__ = "0.2.0"

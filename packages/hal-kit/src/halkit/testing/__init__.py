"""Stdlib test fakes shared by every Hal package.

Import this only from test files (the layering test enforces it, REQ-SYS-04): production code
must never depend on a fake. Nothing here touches the network beyond loopback or a unix socket
in a temporary directory, and nothing needs a GPU.
"""

from .broker import FakeBroker
from .clock import FakeClock
from .home import TempHalHome
from .model_server import FakeModelServer, Reply

__all__ = ["FakeBroker", "FakeClock", "FakeModelServer", "Reply", "TempHalHome"]

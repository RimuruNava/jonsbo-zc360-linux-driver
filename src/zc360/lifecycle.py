"""Process lifecycle helpers shared by the daemon and compatibility API."""

from __future__ import annotations

import signal
import threading


def install_shutdown_handler() -> threading.Event:
    """Request shutdown only at the daemon's next completed frame boundary."""
    requested = threading.Event()

    def handler(_signum, _frame):
        requested.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    return requested


"""Hardware and protocol constants for the ZC-360 display controllers."""

from __future__ import annotations

import os
from pathlib import Path

VENDOR_ID = 0x43A8
PRODUCT_ID = 0x0E61

ENDPOINT_OUT = 0x02
ENDPOINT_IN = 0x82

NATIVE_WIDTH = 180
NATIVE_HEIGHT = 640
LOGICAL_WIDTH = 640
LOGICAL_HEIGHT = 180

PANEL_COUNT = 3
PIXEL_PAYLOAD_SIZE = 480
FRAME_RECORD_SIZE = 512
FRAME_RECORD_COUNT = 720
FRAME_RECORDS_PER_WRITE = 8
FRAME_WRITE_SIZE = 4096
FRAME_WRITE_COUNT = 90

CHANNEL_COMMAND = 0x01
CHANNEL_FRAME = 0x02
DES_KEY = bytes((65, 95, 217, 250, 19, 66, 88, 183))


def socket_path() -> Path:
    """Return the per-user daemon socket path, respecting existing overrides."""
    configured = os.environ.get("JONSBO_FAN_SOCK")
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return Path(runtime) / "jonsbo_fan.sock"


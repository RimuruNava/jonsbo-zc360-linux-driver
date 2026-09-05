#!/usr/bin/env python3
"""Compatibility API for pre-package ZC-360 clients and research scripts.

New applications should import :mod:`zc360`. This module deliberately keeps
the historical names used by the validated async transport and Lucille Shell.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from zc360.constants import (  # noqa: E402
    CHANNEL_COMMAND as CH_CMD,
    CHANNEL_FRAME as CH_FRAME,
    DES_KEY,
    ENDPOINT_IN as EP_IN,
    ENDPOINT_OUT as EP_OUT,
    LOGICAL_HEIGHT as LOGICAL_H,
    LOGICAL_WIDTH as LOGICAL_W,
    NATIVE_HEIGHT as H,
    NATIVE_WIDTH as W,
    PIXEL_PAYLOAD_SIZE as PAYLOAD,
    PRODUCT_ID as PID,
    VENDOR_ID as VID,
    socket_path,
)
from zc360.frames import native_bgr_bytes  # noqa: E402
from zc360.ipc import encode_fan_frames, send_fan_frames, send_fan_packet  # noqa: E402
from zc360.lifecycle import install_shutdown_handler  # noqa: E402
from zc360.protocol import (  # noqa: E402
    build_command_packet,
    build_frame_chunks,
    decrypt_payload,
    decrypt_response,
    encrypt_command,
    frame_header,
)

SOCK_PATH = str(socket_path())
FRAME_RECORDS_PER_WRITE = max(1, int(os.environ.get("JONSBO_FRAME_RECORDS_PER_WRITE", "8")))


def _des_encrypt_cmd(cmd, param=0):
    return encrypt_command(cmd, param)


def _des_decrypt_payload(payload8):
    return decrypt_payload(payload8)


def _build_cmd_packet(cmd, param=0):
    return build_command_packet(cmd, param)


def _frame_header(seq):
    return frame_header(seq)


def _build_frame_chunks(bgr_bytes):
    return build_frame_chunks(bgr_bytes, records_per_write=FRAME_RECORDS_PER_WRITE)


def prepare_frame_chunks(img):
    return _build_frame_chunks(native_bgr_bytes(img))


def install_sigterm_handler():
    return install_shutdown_handler()


class FanPanel:
    """Historical synchronous PyUSB panel API retained for research tools.

    The production daemon does not use this class. It owns all three devices
    through one persistent libusb1 context instead.
    """

    def __init__(self, usb_dev, label=""):
        self.dev = usb_dev
        self.label = label
        self._claimed = False

    def open(self, claim_only=False, settle=0.1):
        import usb.util

        for configuration in self.dev:
            for interface in configuration:
                try:
                    if self.dev.is_kernel_driver_active(interface.bInterfaceNumber):
                        self.dev.detach_kernel_driver(interface.bInterfaceNumber)
                except Exception:
                    pass
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        self._claimed = True
        time.sleep(settle)
        if not claim_only:
            self.init_commands()

    def init_commands(self):
        self._send_cmd(56, 0)
        self._send_cmd(84, 100)
        self._send_cmd(86, 0)

    def close(self):
        if not self._claimed:
            return
        import usb.util

        try:
            usb.util.release_interface(self.dev, 0)
        except Exception:
            pass
        self._claimed = False

    def _send_cmd(self, cmd, param=0):
        import usb.core

        try:
            self.dev.write(EP_OUT, _build_cmd_packet(cmd, param), timeout=2000)
        except usb.core.USBError:
            return None
        time.sleep(0.001)
        try:
            return decrypt_response(bytes(self.dev.read(EP_IN, 32, timeout=100)))
        except usb.core.USBError:
            return None

    def push_image(self, img):
        import usb.core

        for chunk in prepare_frame_chunks(img):
            self.dev.write(EP_OUT, chunk, timeout=3000)
        time.sleep(float(os.environ.get("JONSBO_FRAME_STATUS_GAP_MS", "1.0")) / 1000.0)
        try:
            response = decrypt_response(bytes(self.dev.read(EP_IN, 32, timeout=100)))
        except usb.core.USBError:
            return None
        if response[2] == 0x60:
            self._send_cmd(88, 0)
            time.sleep(0.3)
            return True
        return False

    def warmup(self, img, n=30, delay=0.05):
        commits = 0
        for _ in range(n):
            if self.push_image(img):
                commits += 1
            time.sleep(delay)
        return commits


def find_panels():
    import usb.core

    devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))

    def identity(device):
        try:
            serial = device.serial_number or ""
        except Exception:
            serial = ""
        return serial, device.bus, device.address

    devices.sort(key=identity)
    panels = []
    for index, device in enumerate(devices):
        try:
            serial = device.serial_number or f"panel-{index}"
        except Exception:
            serial = f"panel-{index}"
        panels.append(FanPanel(device, serial))
    return panels

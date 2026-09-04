#!/usr/bin/env python3
"""
Jonsbo TF3 / ZC-360 fan-panel driver library (3x LCD panels, native 180x640
portrait, USB 43a8:0e61).

DES-handshake + commit protocol, reconstructed by analyzing USB captures and
decompiling the vendor Windows app (GClass19.cs). Protocol details/derivation
are in README.md.
"""
import io
import os
import signal
import socket
import struct
import threading
import time
import usb.core, usb.util
from Crypto.Cipher import DES
from PIL import Image

VID, PID = 0x43a8, 0x0e61
SOCK_PATH = os.environ.get(
    "JONSBO_FAN_SOCK",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "jonsbo_fan.sock"),
)
EP_OUT, EP_IN = 0x02, 0x82
PAYLOAD = 480
FRAME_RECORDS_PER_WRITE = max(
    1,
    int(os.environ.get("JONSBO_FRAME_RECORDS_PER_WRITE", "8")),
)
# The physical framebuffer is portrait 180x640 (verified by reconstructing a
# real frame from a USB capture, see README.md) - NOT 640x180 as you'd guess
# from the panel's mounted orientation. The panel controller sits rotated 90
# degrees inside the fan hub; the Windows app rotates content before sending
# accordingly (see GClass11.method_81 RotateFlip). This library accepts
# images in the natural landscape orientation (LOGICAL_W x LOGICAL_H) and
# rotates them into the native portrait buffer before sending.
W, H = 180, 640
LOGICAL_W, LOGICAL_H = 640, 180
CH_FRAME, CH_CMD = 0x02, 0x01
DES_KEY = bytes([65, 95, 217, 250, 19, 66, 88, 183])

def _des_encrypt_cmd(cmd, param=0):
    cipher = DES.new(DES_KEY, DES.MODE_ECB)
    plain = bytes([cmd, param, 0, 0, 0, 0, 0, 0])
    pad = 8 - (len(plain) % 8)
    plain += bytes([pad]) * pad
    return cipher.encrypt(plain)[:8]


def _des_decrypt_payload(payload8):
    cipher = DES.new(DES_KEY, DES.MODE_ECB)
    return cipher.decrypt(payload8)


def _build_cmd_packet(cmd, param=0):
    pkt = bytearray(32)
    pkt[0] = 0xAA
    pkt[1] = 0x55
    pkt[2:10] = _des_encrypt_cmd(cmd, param)
    pkt[10] = CH_CMD
    pkt[31] = 0xBB
    return bytes(pkt)


def _frame_header(seq):
    h = bytearray(32)
    h[0] = 0xAA
    h[1] = 0x55
    h[10] = CH_FRAME
    h[11] = (seq >> 8) & 0xFF
    h[12] = seq & 0xFF
    h[31] = 0xBB
    return bytes(h)


def _build_frame_chunks(bgr_bytes):
    tp = len(bgr_bytes)
    out = []
    seq = 1
    i = 0
    n = tp // PAYLOAD

    while i < n:
        buf = bytearray()
        count = min(FRAME_RECORDS_PER_WRITE, n - i)

        for _ in range(count):
            buf += (
                _frame_header(seq)
                + bgr_bytes[i * PAYLOAD:(i + 1) * PAYLOAD]
            )
            seq += 1
            i += 1

        out.append(bytes(buf))

    return out


def prepare_frame_chunks(img):
    """Convert a logical 640x180 image into the 90 protocol chunks used by
    the ZC-360 framebuffer transport.

    This contains no USB I/O and is shared by both the known-good PyUSB path
    and the async libusb transport.
    """
    if img.size != (LOGICAL_W, LOGICAL_H):
        img = img.resize((LOGICAL_W, LOGICAL_H))

    if img.mode != "RGB":
        img = img.convert("RGB")

    img = img.rotate(-90, expand=True)

    r, g, b = img.split()
    bgr_img = Image.merge("RGB", (b, g, r))

    chunks = _build_frame_chunks(bgr_img.tobytes())

    if FRAME_RECORDS_PER_WRITE == 8:
        if len(chunks) != 90:
            raise RuntimeError(
                f"expected 90 ZC-360 framebuffer writes, got {len(chunks)}"
            )

        if any(len(chunk) != 4096 for chunk in chunks):
            raise RuntimeError(
                "expected all ZC-360 framebuffer writes to be 4096 bytes"
            )

    return chunks


class FanPanel:
    """A single fan-panel display (native 180x640, push_image() takes 640x180 landscape)."""

    def __init__(self, usb_dev, label=""):
        self.dev = usb_dev
        self.label = label
        self._claimed = False

    def open(self, claim_only=False, settle=0.1):
        """Detaches the kernel driver, claims the interface, waits `settle`s
        and (unless claim_only=True) sends the init commands. When driving
        multiple panels on the same (non-switchable) internal hub, claim ALL
        panels first (claim_only=True, settle=0.3ish) and only then call
        init_commands() on all of them - claiming+initing one panel fully
        before touching the next has been observed to leave all panels
        black when 3 are run concurrently."""
        for cfg in self.dev:
            for intf in cfg:
                try:
                    if self.dev.is_kernel_driver_active(intf.bInterfaceNumber):
                        self.dev.detach_kernel_driver(intf.bInterfaceNumber)
                except Exception:
                    pass
        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        self._claimed = True
        time.sleep(settle)
        if not claim_only:
            self.init_commands()

    def init_commands(self):
        self._send_cmd(56, 0)   # init
        self._send_cmd(84, 100)  # brightness
        self._send_cmd(86, 0)   # rotation

    def close(self):
        if self._claimed:
            try:
                usb.util.release_interface(self.dev, 0)
            except Exception:
                pass
            self._claimed = False

    def _send_cmd(self, cmd, param=0):
        pkt = _build_cmd_packet(cmd, param)
        try:
            self.dev.write(EP_OUT, pkt, timeout=2000)
        except usb.core.USBError:
            return None
        time.sleep(0.001)
        try:
            raw = bytearray(self.dev.read(EP_IN, 32, timeout=100))
        except usb.core.USBError:
            return None
        raw[2:10] = _des_decrypt_payload(bytes(raw[2:10]))
        return bytes(raw)

    def push_image(self, img):
        """img: PIL.Image in landscape orientation (any size/mode), gets
        resized to 640x180/RGB, then rotated clockwise into the native
        180x640 portrait buffer and BGR-converted.
        Returns True if a commit (cmd 88) was triggered, False if not,
        None if no interrupt response came back."""
        chunks = prepare_frame_chunks(img)
        for buf in chunks:
            self.dev.write(EP_OUT, buf, timeout=3000)
        frame_gap_ms = float(
            os.environ.get("JONSBO_FRAME_STATUS_GAP_MS", "1.0")
        )
        time.sleep(frame_gap_ms / 1000.0)
        try:
            resp = bytearray(self.dev.read(EP_IN, 32, timeout=100))
        except usb.core.USBError:
            return None
        resp[2:10] = _des_decrypt_payload(bytes(resp[2:10]))
        if resp[2] == 0x60:
            self._send_cmd(88, 0)  # commit - only needed on the very first frame
            time.sleep(0.3)
            return True
        return False

    def warmup(self, img, n=30, delay=0.05):
        """A single push_image() is not enough to switch the panel from its
        boot logo to live content (verified against real hardware) - push
        the same image repeatedly right after open() until it switches over.
        Returns the number of commits triggered along the way."""
        commits = 0
        for _ in range(n):
            if self.push_image(img):
                commits += 1
            time.sleep(delay)
        return commits


def find_panels():
    """Returns FanPanel objects in stable serial-number order.

    USB bus addresses can change across boots. These panels expose individual
    serials, so use those as the durable identity and only fall back to the
    bus/address tuple when descriptor access is unavailable.
    """
    devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))

    def identity(dev):
        try:
            serial = dev.serial_number or ""
        except Exception:
            serial = ""
        return (serial, dev.bus, dev.address)

    devs.sort(key=identity)
    panels = []
    for index, dev in enumerate(devs):
        try:
            serial = dev.serial_number or f"panel-{index}"
        except Exception:
            serial = f"panel-{index}"
        panels.append(FanPanel(dev, serial))
    return panels


def install_sigterm_handler():
    """SIGTERM (from `timeout`, `systemctl stop`, `kill` without -9) is not
    turned into a KeyboardInterrupt by default and can leave a panel mid
    USB-transfer on shutdown - this has previously bricked all 3 panels
    solid black, recoverable only via a physical power cycle (see README.md).

    IMPORTANT: this handler does NOT call close()/release_interface() from
    signal context. An earlier version did, and that most likely caused the
    exact problem it was meant to prevent: if SIGTERM lands during an
    in-flight push_image() chunk-transfer loop, releasing the interface mid
    transfer is structurally the same corruption pattern as a SIGKILL landing
    mid-transfer. This is the likely reason a plain `systemctl restart`
    repeatedly wedged all panels even with a SIGTERM handler installed.

    Instead, this only sets a threading.Event. The caller MUST check this
    event at safe points (between two fully completed push_image() calls,
    NEVER in the middle of an in-flight frame push) and only call close()
    there."""
    shutdown_requested = threading.Event()

    def _handler(signum, frame):
        shutdown_requested.set()
    signal.signal(signal.SIGTERM, _handler)
    return shutdown_requested


def encode_fan_frames(images):
    """Encode a panel-image dict into one daemon socket packet.

    This performs PNG work only. It does not open the Unix socket and cannot
    touch USB. Keeping encoding separate lets the renderer prepare the next
    frame while the previous packet is still being transmitted by another
    thread.
    """
    encode_started = time.monotonic()
    buf = bytearray([len(images)])

    for idx, img in images.items():
        bio = io.BytesIO()
        img.save(bio, "PNG", compress_level=0)
        data = bio.getvalue()
        buf += struct.pack(">BI", idx, len(data)) + data

    packet = bytes(buf)

    return packet, {
        "encode_seconds": time.monotonic() - encode_started,
        "payload_bytes": len(packet),
    }


def send_fan_packet(packet, timeout=120.0):
    """Send one already-encoded daemon packet and wait for its ACK."""
    transfer_started = time.monotonic()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall(packet)
        acknowledgement = s.recv(1)

        if acknowledgement != b"\x01":
            raise RuntimeError("fan daemon returned no acknowledgement")

    return {
        "transfer_seconds": time.monotonic() - transfer_started,
    }


def send_fan_frames(images, timeout=120.0):
    """Compatibility helper preserving the original synchronous API."""
    packet, metrics = encode_fan_frames(images)
    metrics.update(
        send_fan_packet(
            packet,
            timeout=timeout,
        )
    )
    return metrics

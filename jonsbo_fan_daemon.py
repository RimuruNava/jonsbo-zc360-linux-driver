#!/usr/bin/env python3
"""
Jonsbo ZC-360 fan-panel USB daemon.

The daemon is the single long-lived USB owner for all three ZC-360 displays.
Rendering clients send already-rendered images over a Unix socket and never
touch the USB interfaces directly.

USB ownership is now handled by python-libusb1 from first claim through
shutdown. The known-good synchronous path is used for initialization/warmup
and partial-panel updates; complete three-panel packets use the true async
transport reproduced from the official Windows scheduling behaviour.

Hardware rule:
    claim all three once -> init all three -> retain ownership for the whole
    daemon lifetime. Never hand ownership between PyUSB and libusb1 during
    the same physical power session.
"""

import io
import os
import socket
import struct
import sys
import time

import usb1
from PIL import Image, ImageDraw

from jonsbo_fan_lib import (
    LOGICAL_H,
    LOGICAL_W,
    SOCK_PATH,
    install_sigterm_handler,
)
import zc360_async_transport as transport


STALE_AFTER = 30
WARMUP_CYCLES = 30
WARMUP_DELAY = 0.05


def startup_frame(index, label):
    """Visible ownership marker used for the first interleaved warmup."""
    img = Image.new("RGB", (LOGICAL_W, LOGICAL_H), (9, 12, 17))
    draw = ImageDraw.Draw(img)

    accent = (
        (255, 96, 180),
        (94, 226, 255),
        (255, 210, 84),
    )[index % 3]

    draw.rectangle(
        (0, 0, LOGICAL_W - 1, LOGICAL_H - 1),
        outline=accent,
        width=8,
    )
    draw.rectangle(
        (24, 26, 34, LOGICAL_H - 27),
        fill=accent,
    )
    draw.text(
        (56, 47),
        f"ZC-360  PANEL {index}",
        fill=(235, 239, 244),
    )
    draw.text(
        (56, 88),
        label,
        fill=accent,
    )
    draw.text(
        (56, 127),
        "USB OWNER ONLINE",
        fill=(150, 158, 171),
    )

    return img


def recv_exact(conn, n):
    buf = b""

    while len(buf) < n:
        chunk = conn.recv(n - len(buf))

        if not chunk:
            return None

        buf += chunk

    return buf


def recv_frames(conn):
    """Read one socket frame packet.

    Format:
        1 byte panel count
        repeated:
            1 byte panel index
            4 byte big-endian PNG length
            PNG bytes
    """
    head = recv_exact(conn, 1)

    if head is None:
        return None

    count = head[0]
    out = {}

    for _ in range(count):
        header = recv_exact(conn, 5)

        if header is None:
            return None

        idx, length = struct.unpack(">BI", header)
        data = recv_exact(conn, length)

        if data is None:
            return None

        out[idx] = Image.open(
            io.BytesIO(data)
        ).convert("RGB")

    return out


def wait_for_usb_access(context, timeout=15.0, interval=0.25):
    """Wait for udev permissions without opening or claiming any interface."""
    deadline = time.monotonic() + timeout
    last_status = None

    while True:
        devices = []

        for dev in context.getDeviceIterator(skip_on_error=True):
            if (
                dev.getVendorID() == 0x43A8
                and dev.getProductID() == 0x0E61
            ):
                devices.append(dev)

        paths = []
        ready = len(devices) == transport.EXPECTED_PANELS

        if ready:
            for dev in devices:
                bus = dev.getBusNumber()
                address = dev.getDeviceAddress()

                device_path = (
                    f"/dev/bus/usb/{bus:03d}/{address:03d}"
                )
                paths.append(device_path)

                if not os.access(
                    device_path,
                    os.R_OK | os.W_OK,
                ):
                    ready = False

        status = (
            len(devices),
            tuple(sorted(paths)),
            ready,
        )

        if status != last_status:
            print(
                f"USB startup gate: "
                f"found={len(devices)}/3 "
                f"accessible={'yes' if ready else 'no'}",
                flush=True,
            )
            last_status = status

        if ready:
            print(
                "USB startup gate: all three panels are accessible; "
                "continuing with one libusb1 claim cycle.",
                flush=True,
            )
            return

        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Timed out waiting for all three ZC-360 USB devices "
                "to become accessible"
            )

        time.sleep(interval)


def warmup_subset(handles, frames):
    """Warm the panels represented by this socket packet in lockstep.

    Startup uses transport.warmup_interleaved() for exactly three panels.
    This helper preserves the old daemon's behaviour for a stale/fresh
    partial-panel packet without changing USB ownership.
    """
    prepared = {}

    for idx, img in frames.items():
        if idx >= len(handles):
            continue

        prepared[idx] = transport.prepare_image_chunks(img)

    for _ in range(WARMUP_CYCLES):
        for idx in sorted(prepared):
            transport.push_chunks_sync(
                handles[idx],
                prepared[idx],
            )

        time.sleep(WARMUP_DELAY)


def process_frames(
    context,
    handles,
    frames,
    warmed,
    last_seen,
):
    """Process one client packet using the persistent libusb1 owner.

    Complete 0/1/2 triplets use the true async transport.
    Partial packets remain on the conservative synchronous path.
    """
    packet_started = time.perf_counter()
    now = time.time()

    valid_frames = {
        idx: img
        for idx, img in frames.items()
        if idx < len(handles)
    }

    if not valid_frames:
        return

    needs_warmup = any(
        not warmed[idx]
        or (now - last_seen[idx]) > STALE_AFTER
        for idx in valid_frames
    )

    if needs_warmup:
        warmup_subset(
            handles,
            valid_frames,
        )

        for idx in valid_frames:
            warmed[idx] = True

        mode = "serial-warmup"

    elif (
        len(valid_frames) == transport.EXPECTED_PANELS
        and all(
            idx in valid_frames
            for idx in range(transport.EXPECTED_PANELS)
        )
    ):
        prepared = transport.prepare_triplet(
            [
                valid_frames[0],
                valid_frames[1],
                valid_frames[2],
            ]
        )

        metrics = transport.async_triplet(
            context,
            handles,
            prepared,
            frame_gap_ms=float(
                os.environ.get(
                    "JONSBO_ASYNC_FRAME_GAP_MS",
                    str(transport.DEFAULT_FRAME_GAP_MS),
                )
            ),
            first_submit_stagger_ms=float(
                os.environ.get(
                    "JONSBO_ASYNC_FIRST_SUBMIT_STAGGER_MS",
                    str(
                        transport.DEFAULT_FIRST_SUBMIT_STAGGER_MS
                    ),
                )
            ),
            report=(
                os.environ.get(
                    "JONSBO_ASYNC_REPORT",
                    "0",
                )
                == "1"
            ),
        )

        mode = (
            "async-triplet "
            f"usb={metrics['total_ms']:.2f}ms "
            f"status="
            + "/".join(
                f"0x{x:02x}"
                for x in metrics["statuses"]
            )
        )

    else:
        per_panel = []

        for idx in sorted(valid_frames):
            started = time.perf_counter()
            status = transport.push_image_sync(
                handles[idx],
                valid_frames[idx],
            )
            elapsed_ms = (
                time.perf_counter() - started
            ) * 1000.0

            per_panel.append(
                f"{idx}:{elapsed_ms:.2f}ms/"
                + (
                    "none"
                    if status is None
                    else f"0x{status:02x}"
                )
            )

        mode = "serial-partial " + ",".join(per_panel)

    finished = time.time()

    for idx in valid_frames:
        last_seen[idx] = finished

    packet_ms = (
        time.perf_counter() - packet_started
    ) * 1000.0

    print(
        f"USB PERF packet={packet_ms:.2f}ms "
        f"panels={len(valid_frames)} "
        f"mode={mode}",
        flush=True,
    )


def remove_stale_socket():
    """Refuse to replace a socket that still belongs to a live owner."""
    if not os.path.exists(SOCK_PATH):
        return

    probe = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )
    probe.settimeout(0.25)

    try:
        probe.connect(SOCK_PATH)
    except OSError:
        try:
            os.remove(SOCK_PATH)
        except FileNotFoundError:
            pass
        return
    finally:
        probe.close()

    raise RuntimeError(
        f"{SOCK_PATH} already has a live listener. "
        "Refusing to start a second ZC-360 USB owner."
    )


def main():
    print(
        "Jonsbo ZC-360 libusb1 USB owner - "
        "keeps 3 panels claimed, Ctrl+C to stop",
        flush=True,
    )

    context = usb1.USBContext()
    handles = []
    server = None

    shutdown_requested = install_sigterm_handler()

    try:
        # This check happens before any USB claim.
        remove_stale_socket()

        wait_for_usb_access(context)

        found = transport.discover_panels(context)

        labels = []

        print("found panels:", flush=True)

        for idx, item in enumerate(found):
            serial, bus, address, _ = item
            label = serial or f"panel-{idx}"
            labels.append(label)

            print(
                f"  {idx}: {label} "
                f"bus={bus} address={address}",
                flush=True,
            )

        # The critical lifecycle:
        # claim ALL -> init ALL -> never hand ownership to another backend.
        handles = transport.claim_all(found)
        transport.init_all(handles)

        startup_images = [
            startup_frame(index, label)
            for index, label in enumerate(labels)
        ]

        transport.warmup_interleaved(
            handles,
            startup_images,
            n=WARMUP_CYCLES,
            delay=WARMUP_DELAY,
        )

        warmed = [True] * len(handles)
        last_seen = [time.time()] * len(handles)

        print(
            "All three panels initialized and warmed "
            "on persistent libusb1 handles.",
            flush=True,
        )

        server = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        server.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        server.listen(1)
        server.settimeout(1.0)

        print(
            f"Socket ready: {SOCK_PATH}",
            flush=True,
        )

        while not shutdown_requested.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with conn:
                try:
                    frames = recv_frames(conn)
                except Exception as exc:
                    print(
                        f"Receive error: {exc}",
                        flush=True,
                    )
                    continue

                if not frames:
                    continue

                try:
                    process_frames(
                        context,
                        handles,
                        frames,
                        warmed,
                        last_seen,
                    )
                except Exception as exc:
                    print(
                        f"Error while processing: {exc}",
                        flush=True,
                    )

                try:
                    conn.sendall(b"\x01")
                except OSError:
                    pass

    except KeyboardInterrupt:
        print(
            "Keyboard interrupt requested.",
            flush=True,
        )

    finally:
        # process_frames()/async_triplet() are synchronous from the daemon's
        # point of view: this finally block is only reached after any current
        # framebuffer/status barrier has completed.
        print(
            "Shutting down cleanly; "
            "no async triplet is in flight.",
            flush=True,
        )

        if server is not None:
            try:
                server.close()
            except OSError:
                pass

        if os.path.exists(SOCK_PATH):
            try:
                os.remove(SOCK_PATH)
            except OSError:
                pass

        transport.release_all(handles)

        try:
            context.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

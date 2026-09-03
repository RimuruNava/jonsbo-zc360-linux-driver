"""
EXPERIMENTAL / UNSAFE TRANSPORT

This module is retained for reverse-engineering history only.

It attempts to reproduce the official Windows three-panel scheduling using
three Python threads issuing synchronous blocking PyUSB transfers.

Although usbmon superficially resembled the vendor transport and throughput
improved substantially, this implementation produced persistent stale/partial
framebuffer regions on real ZC-360 hardware during testing.

Affected displays could retain portions of the built-in Jonsbo logo or
previous framebuffer content. Once the controller entered this state,
subsequent otherwise-valid framebuffer writes did not reliably recover it;
a full physical power cycle was sometimes required.

DO NOT use this implementation as the production three-panel transport.

The validated direction is genuine asynchronous libusb transfers using
python-libusb1. See:

    docs/ZC360_PROTOCOL_RESEARCH.md
    docs/ZC360_EXPERIMENT_LOG.md
    async_triplet_probe.py
"""

import os
import threading
import time

from PIL import Image

import jonsbo_fan_lib as fanlib


def process_vendor_triplet(panels, frames):
    """Reproduce the Windows three-panel steady-state frame schedule.

    Observed Windows behaviour:

      * exactly 90 x 4096-byte bulk OUT transfers per panel
      * one transfer may be outstanding on each physical panel
      * all three framebuffer streams must reach the SAME frame boundary
      * ~12.7 ms quiet period after the last framebuffer OUT completes
      * then all three 32-byte status IN reads are started together
      * next frame cannot start until all three status reads finish

    This deliberately does NOT use FanPanel.push_image(), because that method
    reads status as soon as its individual panel finishes its framebuffer.
    """

    if len(panels) != 3 or any(idx not in frames for idx in range(3)):
        raise RuntimeError(
            "vendor triplet mode requires frames for panels 0, 1 and 2"
        )

    # The Windows capture uses 8 protocol records per USB write:
    #
    #   8 * (32-byte header + 480-byte payload) == 4096 bytes
    #
    # Do not silently run this experiment with another batching size.
    if fanlib.FRAME_RECORDS_PER_WRITE != 8:
        raise RuntimeError(
            "vendor triplet mode requires "
            "JONSBO_FRAME_RECORDS_PER_WRITE=8"
        )

    #
    # Prepare all three complete frame buffers BEFORE touching USB.
    #
    prepared = []

    for idx in range(3):
        img = frames[idx]

        if img.size != (fanlib.LOGICAL_W, fanlib.LOGICAL_H):
            img = img.resize(
                (fanlib.LOGICAL_W, fanlib.LOGICAL_H)
            )

        if img.mode != "RGB":
            img = img.convert("RGB")

        # Same transformation as FanPanel.push_image().
        img = img.rotate(-90, expand=True)

        r, g, b = img.split()
        bgr_img = Image.merge("RGB", (b, g, r))

        chunks = fanlib._build_frame_chunks(
            bgr_img.tobytes()
        )

        if len(chunks) != 90:
            raise RuntimeError(
                f"panel {idx}: expected 90 frame chunks, "
                f"got {len(chunks)}"
            )

        bad_sizes = [
            len(chunk)
            for chunk in chunks
            if len(chunk) != 4096
        ]

        if bad_sizes:
            raise RuntimeError(
                f"panel {idx}: expected only 4096-byte chunks; "
                f"saw {bad_sizes[:4]}"
            )

        prepared.append(chunks)

    frame_started = time.perf_counter()

    #
    # FRAMEBUFFER PHASE
    #
    # Three workers, one per physical USB device.
    #
    # Each worker keeps exactly one synchronous bulk transfer outstanding:
    #
    #     submit 4K
    #     wait for completion
    #     immediately submit next 4K
    #
    # With three workers this lets libusb/xHCI interleave the devices in the
    # same basic pattern seen in the Windows USBPcap trace.
    #
    write_gate = threading.Barrier(4)

    write_errors = [None, None, None]
    write_done = [0.0, 0.0, 0.0]

    def write_worker(idx):
        try:
            write_gate.wait()

            for chunk in prepared[idx]:
                written = panels[idx].dev.write(
                    fanlib.EP_OUT,
                    chunk,
                    timeout=3000,
                )

                if written != len(chunk):
                    raise RuntimeError(
                        f"short write "
                        f"{written}/{len(chunk)} bytes"
                    )

            write_done[idx] = time.perf_counter()

        except BaseException as exc:
            write_errors[idx] = exc

    write_threads = [
        threading.Thread(
            target=write_worker,
            args=(idx,),
            name=f"zc360-vendor-out-{idx}",
            daemon=False,
        )
        for idx in range(3)
    ]

    for thread in write_threads:
        thread.start()

    # Release all three workers from the start gate together.
    write_gate.wait()

    for thread in write_threads:
        thread.join()

    if any(error is not None for error in write_errors):
        detail = "; ".join(
            f"panel {idx}: {error}"
            for idx, error in enumerate(write_errors)
            if error is not None
        )

        raise RuntimeError(
            f"vendor triplet frame-write failure: {detail}"
        )

    #
    # THREE-PANEL FRAME BARRIER.
    #
    # Absolutely no status read — and especially no next framebuffer — may
    # start until every panel has finished chunk 90.
    #
    writes_complete = max(write_done)

    #
    # Windows capture:
    #
    # last framebuffer completion ~0.037309
    # first status submission     ~0.050026
    #
    # ~= 12.7 ms controller quiet period.
    #
    gap_ms = float(
        os.environ.get(
            "JONSBO_VENDOR_FRAME_GAP_MS",
            "12.7",
        )
    )

    remaining = (
        writes_complete
        + gap_ms / 1000.0
        - time.perf_counter()
    )

    if remaining > 0:
        time.sleep(remaining)

    #
    # STATUS PHASE
    #
    # Windows starts all three reads essentially together.
    #
    read_gate = threading.Barrier(4)

    read_errors = [None, None, None]
    read_started = [0.0, 0.0, 0.0]
    read_done = [0.0, 0.0, 0.0]
    responses = [None, None, None]

    def read_worker(idx):
        try:
            read_gate.wait()

            read_started[idx] = time.perf_counter()

            raw = bytearray(
                panels[idx].dev.read(
                    fanlib.EP_IN,
                    32,
                    timeout=100,
                )
            )

            if len(raw) != 32:
                raise RuntimeError(
                    f"short status read {len(raw)}/32 bytes"
                )

            raw[2:10] = fanlib._des_decrypt_payload(
                bytes(raw[2:10])
            )

            responses[idx] = bytes(raw)
            read_done[idx] = time.perf_counter()

        except BaseException as exc:
            read_errors[idx] = exc

    read_threads = [
        threading.Thread(
            target=read_worker,
            args=(idx,),
            name=f"zc360-vendor-in-{idx}",
            daemon=False,
        )
        for idx in range(3)
    ]

    for thread in read_threads:
        thread.start()

    # Release all three status-read workers together.
    read_gate.wait()

    for thread in read_threads:
        thread.join()

    if any(error is not None for error in read_errors):
        detail = "; ".join(
            f"panel {idx}: {error}"
            for idx, error in enumerate(read_errors)
            if error is not None
        )

        raise RuntimeError(
            f"vendor triplet status-read failure: {detail}"
        )

    #
    # STATUS BARRIER.
    #
    # At this point all three responses are complete. Only now may this frame
    # transaction end.
    #
    statuses = [
        response[2]
        for response in responses
    ]

    #
    # Normally warmup has already handled the initial 0x60 state. Keep the
    # protocol rule here anyway so experimental mode is self-consistent.
    #
    commits = 0

    for idx, status in enumerate(statuses):
        if status == 0x60:
            result = panels[idx]._send_cmd(88, 0)

            if result is None:
                raise RuntimeError(
                    f"panel {idx}: commit command 88 failed"
                )

            commits += 1

    if commits:
        time.sleep(0.3)

    frame_finished = time.perf_counter()

    write_ms = (
        writes_complete - frame_started
    ) * 1000.0

    actual_gap_ms = (
        min(read_started) - writes_complete
    ) * 1000.0

    status_ms = (
        max(read_done) - min(read_started)
    ) * 1000.0

    total_ms = (
        frame_finished - frame_started
    ) * 1000.0

    print(
        "VENDOR TRIPLET "
        f"write={write_ms:.2f}ms "
        f"gap={actual_gap_ms:.2f}ms "
        f"status={status_ms:.2f}ms "
        f"total={total_ms:.2f}ms "
        f"fps={1000.0 / total_ms:.2f} "
        f"status={'/'.join(f'0x{x:02x}' for x in statuses)} "
        f"commits={commits}",
        flush=True,
    )

    return statuses

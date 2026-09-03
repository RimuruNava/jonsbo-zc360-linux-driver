#!/usr/bin/env python3
"""
Persistent libusb1 transport for the Jonsbo ZC-360 fan LCDs.

This module is extracted from async_triplet_probe.py, which produced the
known-clean ~16.8 FPS three-panel result.

Important hardware rule:
    claim all three panels once, initialize them all, and retain ownership
    for the lifetime of the daemon. Do not hand ownership between PyUSB and
    libusb1 during the same physical power session.

This module performs no USB I/O merely by being imported.
"""

import time

import usb1

import jonsbo_fan_lib as fanlib


EXPECTED_PANELS = 3
EXPECTED_CHUNKS = 90
EXPECTED_CHUNK_SIZE = 4096

DEFAULT_FRAME_GAP_MS = 12.7
DEFAULT_FIRST_SUBMIT_STAGGER_MS = 0.5


def validate_async_chunks(chunks):
    """Require the exact transport shape proven by the async probe."""
    if fanlib.FRAME_RECORDS_PER_WRITE != 8:
        raise RuntimeError(
            "async transport requires "
            "JONSBO_FRAME_RECORDS_PER_WRITE=8"
        )

    if len(chunks) != EXPECTED_CHUNKS:
        raise RuntimeError(
            f"expected {EXPECTED_CHUNKS} framebuffer writes, "
            f"got {len(chunks)}"
        )

    for chunk_index, chunk in enumerate(chunks):
        if len(chunk) != EXPECTED_CHUNK_SIZE:
            raise RuntimeError(
                f"chunk {chunk_index}: expected "
                f"{EXPECTED_CHUNK_SIZE} bytes, got {len(chunk)}"
            )


def prepare_image_chunks(img):
    """Convert one logical image into the proven 90x4096 async frame."""
    chunks = fanlib.prepare_frame_chunks(img)
    validate_async_chunks(chunks)
    return chunks


def discover_panels(context):
    """Find the three ZC-360 controllers in stable serial-number order."""
    found = []

    for dev in context.getDeviceIterator(skip_on_error=True):
        if (
            dev.getVendorID() != fanlib.VID
            or dev.getProductID() != fanlib.PID
        ):
            continue

        try:
            serial = dev.getSerialNumber() or ""
        except Exception:
            serial = ""

        found.append(
            (
                serial,
                dev.getBusNumber(),
                dev.getDeviceAddress(),
                dev,
            )
        )

    found.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
        )
    )

    if len(found) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected exactly {EXPECTED_PANELS} ZC-360 panels, "
            f"found {len(found)}"
        )

    return found


def claim_all(found, settle=0.3):
    """Open and claim ALL panels before any initialization commands."""
    handles = []

    try:
        for idx, (serial, bus, address, dev) in enumerate(found):
            handle = dev.open()

            try:
                if handle.kernelDriverActive(0):
                    handle.detachKernelDriver(0)
            except usb1.USBError:
                pass

            handle.setConfiguration(1)
            handle.claimInterface(0)

            handles.append(handle)

            print(
                f"claimed panel {idx}: "
                f"{serial or '?'} "
                f"bus={bus} address={address}",
                flush=True,
            )

            # Match the known-good Linux claim behaviour.
            time.sleep(settle)

        return handles

    except BaseException:
        release_all(handles)
        raise


def release_all(handles):
    """Release interfaces only after all transfers have fully drained."""
    for handle in handles:
        try:
            handle.releaseInterface(0)
        except Exception:
            pass


def send_cmd_sync(handle, cmd, param=0):
    """Known-good synchronous command path used during init/commit."""
    packet = fanlib._build_cmd_packet(cmd, param)

    written = handle.bulkWrite(
        fanlib.EP_OUT,
        packet,
        timeout=2000,
    )

    if written != len(packet):
        raise RuntimeError(
            f"command {cmd}: short write "
            f"{written}/{len(packet)}"
        )

    time.sleep(0.001)

    try:
        raw = bytearray(
            handle.bulkRead(
                fanlib.EP_IN,
                32,
                timeout=100,
            )
        )
    except usb1.USBError as exc:
        # Match FanPanel._send_cmd(): a missing/late status response during
        # controller initialization is not fatal.
        print(
            f"command {cmd}: status read unavailable "
            f"({exc}); continuing",
            flush=True,
        )
        return None

    if len(raw) != 32:
        print(
            f"command {cmd}: short response "
            f"{len(raw)}/32; continuing",
            flush=True,
        )
        return None

    raw[2:10] = fanlib._des_decrypt_payload(
        bytes(raw[2:10])
    )

    return bytes(raw)


def init_all(handles):
    """Initialize only after all three physical interfaces are claimed."""
    if len(handles) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected {EXPECTED_PANELS} claimed handles, "
            f"got {len(handles)}"
        )

    for idx, handle in enumerate(handles):
        send_cmd_sync(handle, 56, 0)
        send_cmd_sync(handle, 84, 100)
        send_cmd_sync(handle, 86, 0)

        print(
            f"initialized panel {idx}",
            flush=True,
        )


def push_chunks_sync(handle, chunks):
    """Known-good serial framebuffer path on the SAME libusb1 handle."""
    validate_async_chunks(chunks)

    for chunk in chunks:
        written = handle.bulkWrite(
            fanlib.EP_OUT,
            chunk,
            timeout=3000,
        )

        if written != len(chunk):
            raise RuntimeError(
                f"serial short write {written}/{len(chunk)}"
            )

    time.sleep(0.001)

    try:
        raw = bytearray(
            handle.bulkRead(
                fanlib.EP_IN,
                32,
                timeout=100,
            )
        )
    except usb1.USBError:
        # Match FanPanel.push_image(): a missed status response does not
        # invalidate the framebuffer transfer.
        return None

    if len(raw) != 32:
        return None

    raw[2:10] = fanlib._des_decrypt_payload(
        bytes(raw[2:10])
    )

    status = raw[2]

    if status == 0x60:
        send_cmd_sync(handle, 88, 0)
        time.sleep(0.3)

    return status


def push_image_sync(handle, img):
    """Prepare and serially push one image on an already-owned handle."""
    return push_chunks_sync(
        handle,
        prepare_image_chunks(img),
    )


def warmup_interleaved(handles, images, n=30, delay=0.05):
    """Safe startup warmup: panel 0 -> 1 -> 2, repeated in lockstep."""
    if len(handles) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected {EXPECTED_PANELS} handles, got {len(handles)}"
        )

    if len(images) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected {EXPECTED_PANELS} warmup images, got {len(images)}"
        )

    prepared = [
        prepare_image_chunks(img)
        for img in images
    ]

    statuses = [None] * EXPECTED_PANELS

    for _ in range(n):
        for idx in range(EXPECTED_PANELS):
            statuses[idx] = push_chunks_sync(
                handles[idx],
                prepared[idx],
            )

        time.sleep(delay)

    return statuses


def async_triplet(
    context,
    handles,
    prepared,
    frame_gap_ms=DEFAULT_FRAME_GAP_MS,
    first_submit_stagger_ms=DEFAULT_FIRST_SUBMIT_STAGGER_MS,
    report=False,
):
    """Send one complete three-panel frame using genuine libusb async I/O.

    `prepared` must contain exactly three 90x4096 chunk lists.

    Scheduling is intentionally kept extremely close to the clean hardware
    probe:
      * one outstanding OUT transfer per physical panel
      * callback immediately re-arms that panel's next 4096-byte chunk
      * all three panels meet at a framebuffer barrier
      * ~12.7 ms controller quiet period
      * all three status IN transfers are submitted before event handling
      * status barrier before returning
    """
    if len(handles) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected {EXPECTED_PANELS} handles, got {len(handles)}"
        )

    if len(prepared) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected {EXPECTED_PANELS} prepared frames, got {len(prepared)}"
        )

    for chunks in prepared:
        validate_async_chunks(chunks)

    # Exactly one asynchronous OUT transfer object per physical panel.
    next_chunk = [0, 0, 0]
    write_done = [None, None, None]
    write_error = [None, None, None]

    transfers = []
    callbacks = []

    frame_started = time.perf_counter()

    def make_write_callback(idx):
        def callback(transfer):
            try:
                status = transfer.getStatus()

                if status != usb1.TRANSFER_COMPLETED:
                    raise RuntimeError(
                        f"OUT transfer status={status}"
                    )

                expected_len = len(
                    prepared[idx][next_chunk[idx]]
                )

                actual_len = transfer.getActualLength()

                if actual_len != expected_len:
                    raise RuntimeError(
                        f"OUT short completion "
                        f"{actual_len}/{expected_len}"
                    )

                next_chunk[idx] += 1

                if next_chunk[idx] == len(prepared[idx]):
                    write_done[idx] = time.perf_counter()
                    return

                # Re-arm THIS SAME transfer immediately with the next
                # 4096-byte chunk for THIS SAME physical panel.
                transfer.setBulk(
                    fanlib.EP_OUT,
                    prepared[idx][next_chunk[idx]],
                    callback=callback,
                    timeout=3000,
                )
                transfer.submit()

            except BaseException as exc:
                write_error[idx] = exc

        return callback

    for idx, handle in enumerate(handles):
        transfer = handle.getTransfer()
        callback = make_write_callback(idx)

        transfer.setBulk(
            fanlib.EP_OUT,
            prepared[idx][0],
            callback=callback,
            timeout=3000,
        )

        transfers.append(transfer)
        callbacks.append(callback)

    # Preserve the small phase offset observed in Windows steady state.
    submit_times = []

    for idx, transfer in enumerate(transfers):
        submit_times.append(time.perf_counter())
        transfer.submit()

        if idx != len(transfers) - 1:
            time.sleep(first_submit_stagger_ms / 1000.0)

    # One libusb event loop drives all three devices.
    while True:
        terminal = [
            write_done[idx] is not None
            or write_error[idx] is not None
            for idx in range(EXPECTED_PANELS)
        ]

        if all(terminal):
            break

        context.handleEvents()

    if any(error is not None for error in write_error):
        detail = "; ".join(
            f"panel {idx}: {error}"
            for idx, error in enumerate(write_error)
            if error is not None
        )
        raise RuntimeError(
            f"async framebuffer failure: {detail}"
        )

    writes_complete = max(write_done)

    # Three-panel framebuffer barrier, then controller quiet period.
    remaining = (
        writes_complete
        + frame_gap_ms / 1000.0
        - time.perf_counter()
    )

    if remaining > 0:
        time.sleep(remaining)

    # STATUS PHASE:
    # submit all three IN URBs BEFORE processing completion events.
    responses = [None, None, None]
    read_done = [None, None, None]
    read_error = [None, None, None]

    read_transfers = []
    read_callbacks = []

    def make_read_callback(idx):
        def callback(transfer):
            try:
                status = transfer.getStatus()

                if status != usb1.TRANSFER_COMPLETED:
                    raise RuntimeError(
                        f"IN transfer status={status}"
                    )

                actual = transfer.getActualLength()

                if actual != 32:
                    raise RuntimeError(
                        f"IN short completion {actual}/32"
                    )

                raw = bytearray(
                    bytes(transfer.getBuffer()[:actual])
                )

                raw[2:10] = fanlib._des_decrypt_payload(
                    bytes(raw[2:10])
                )

                responses[idx] = bytes(raw)
                read_done[idx] = time.perf_counter()

            except BaseException as exc:
                read_error[idx] = exc

        return callback

    for idx, handle in enumerate(handles):
        transfer = handle.getTransfer()
        callback = make_read_callback(idx)

        transfer.setBulk(
            fanlib.EP_IN,
            32,
            callback=callback,
            timeout=100,
        )

        read_transfers.append(transfer)
        read_callbacks.append(callback)

    read_submit_times = []

    for transfer in read_transfers:
        read_submit_times.append(time.perf_counter())
        transfer.submit()

    while True:
        terminal = [
            read_done[idx] is not None
            or read_error[idx] is not None
            for idx in range(EXPECTED_PANELS)
        ]

        if all(terminal):
            break

        context.handleEvents()

    if any(error is not None for error in read_error):
        detail = "; ".join(
            f"panel {idx}: {error}"
            for idx, error in enumerate(read_error)
            if error is not None
        )
        raise RuntimeError(
            f"async status failure: {detail}"
        )

    statuses = [
        response[2]
        for response in responses
    ]

    commits = 0

    for idx, status in enumerate(statuses):
        if status == 0x60:
            send_cmd_sync(
                handles[idx],
                88,
                0,
            )
            commits += 1

    if commits:
        time.sleep(0.3)

    frame_finished = time.perf_counter()

    metrics = {
        "write_ms": (
            writes_complete - frame_started
        ) * 1000.0,
        "first_submit_span_ms": (
            max(submit_times) - min(submit_times)
        ) * 1000.0,
        "frame_status_gap_ms": (
            min(read_submit_times) - writes_complete
        ) * 1000.0,
        "status_ms": (
            max(read_done) - min(read_submit_times)
        ) * 1000.0,
        "status_submit_span_ms": (
            max(read_submit_times) - min(read_submit_times)
        ) * 1000.0,
        "total_ms": (
            frame_finished - frame_started
        ) * 1000.0,
        "statuses": statuses,
        "commits": commits,
    }

    if report:
        total_ms = metrics["total_ms"]

        print(
            "ASYNC PERF "
            f"write={metrics['write_ms']:.3f}ms "
            f"first_submit_span={metrics['first_submit_span_ms']:.3f}ms "
            f"gap={metrics['frame_status_gap_ms']:.3f}ms "
            f"status={metrics['status_ms']:.3f}ms "
            f"status_submit_span={metrics['status_submit_span_ms']:.3f}ms "
            f"total={total_ms:.3f}ms "
            f"fps={1000.0 / total_ms:.2f} "
            f"statuses={'/'.join(f'0x{x:02x}' for x in statuses)} "
            f"commits={commits}",
            flush=True,
        )

    return metrics


def prepare_triplet(images):
    """Prepare exactly three images for async_triplet()."""
    if len(images) != EXPECTED_PANELS:
        raise RuntimeError(
            f"expected {EXPECTED_PANELS} images, got {len(images)}"
        )

    return [
        prepare_image_chunks(img)
        for img in images
    ]

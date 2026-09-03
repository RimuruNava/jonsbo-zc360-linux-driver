#!/usr/bin/env python3

import argparse
import os
import sys
import time

import usb1
from PIL import Image

import jonsbo_fan_lib as fanlib


EXPECTED_PANELS = 3
EXPECTED_CHUNKS = 90
EXPECTED_CHUNK_SIZE = 4096

DEFAULT_FRAME_GAP_MS = 12.7
DEFAULT_FIRST_SUBMIT_STAGGER_MS = 0.5


def prepare_image(colour):
    img = Image.new(
        "RGB",
        (fanlib.LOGICAL_W, fanlib.LOGICAL_H),
        colour,
    )

    img = img.rotate(-90, expand=True)

    r, g, b = img.split()
    bgr = Image.merge("RGB", (b, g, r))

    chunks = fanlib._build_frame_chunks(bgr.tobytes())

    validate_chunks(chunks)

    return chunks


def validate_chunks(chunks):
    if len(chunks) != EXPECTED_CHUNKS:
        raise RuntimeError(
            f"expected {EXPECTED_CHUNKS} chunks, got {len(chunks)}"
        )

    seqs = []

    for chunk_index, chunk in enumerate(chunks):
        if len(chunk) != EXPECTED_CHUNK_SIZE:
            raise RuntimeError(
                f"chunk {chunk_index}: expected "
                f"{EXPECTED_CHUNK_SIZE} bytes, got {len(chunk)}"
            )

        for record_index in range(8):
            off = record_index * 512
            rec = chunk[off:off + 512]

            if len(rec) != 512:
                raise RuntimeError(
                    f"chunk {chunk_index} record {record_index}: "
                    f"short record"
                )

            header = rec[:32]

            if header[0:2] != b"\xaa\x55":
                raise RuntimeError("bad frame magic")

            if header[10] != fanlib.CH_FRAME:
                raise RuntimeError("bad frame channel")

            if header[31] != 0xBB:
                raise RuntimeError("bad frame terminator")

            seq = (header[11] << 8) | header[12]
            seqs.append(seq)

    expected = list(range(1, 721))

    if seqs != expected:
        raise RuntimeError(
            "frame sequence is not exactly 1..720"
        )


def dry_run():
    prepared = [
        prepare_image((70, 70, 70)),
        prepare_image((70, 70, 70)),
        prepare_image((70, 70, 70)),
    ]

    print("DRY RUN PASS")
    print()

    for idx, chunks in enumerate(prepared):
        first = []
        last = []

        for chunk in chunks[:8]:
            first.append((chunk[11] << 8) | chunk[12])

        for chunk in chunks[-8:]:
            last.append((chunk[11] << 8) | chunk[12])

        print(f"panel {idx}:")
        print(f"  chunks: {len(chunks)}")
        print(f"  bytes:  {sum(map(len, chunks))}")
        print(f"  first chunk seqs: {first}")
        print(f"  last chunk seqs:  {last}")

    print()
    print("total USB framebuffer payload:")
    print(
        f"  {EXPECTED_PANELS} panels × "
        f"{EXPECTED_CHUNKS} writes × "
        f"{EXPECTED_CHUNK_SIZE} bytes"
    )
    print(
        "  =",
        EXPECTED_PANELS
        * EXPECTED_CHUNKS
        * EXPECTED_CHUNK_SIZE,
        "bytes",
    )
    print()
    print("No USB devices were opened.")


def discover_panels(context):
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
            f"expected exactly 3 ZC-360 panels, found {len(found)}"
        )

    return found


def claim_all(found):
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
            time.sleep(0.3)

        return handles

    except BaseException:
        for handle in handles:
            try:
                handle.releaseInterface(0)
            except Exception:
                pass
        raise


def send_cmd_sync(handle, cmd, param=0):
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
        # Match FanPanel._send_cmd(): a missing/late status response
        # is not fatal during controller initialization.
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
    # ALL handles are already claimed before this begins.
    for idx, handle in enumerate(handles):
        send_cmd_sync(handle, 56, 0)
        send_cmd_sync(handle, 84, 100)
        send_cmd_sync(handle, 86, 0)

        print(
            f"initialized panel {idx}",
            flush=True,
        )


def push_chunks_sync(handle, chunks):
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
        # Match FanPanel.push_image(): a missed status response
        # does not invalidate the framebuffer transfer.
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


def serial_seed(handles):
    # Deliberately distinctive previous framebuffer:
    # any stale region after the async gray frame will be obvious.
    colours = [
        (220, 20, 20),   # red
        (20, 220, 20),   # green
        (20, 20, 220),   # blue
    ]

    prepared = [
        prepare_image(colour)
        for colour in colours
    ]

    print("serial warmup/seed starting...", flush=True)

    for cycle in range(30):
        for idx in range(3):
            push_chunks_sync(
                handles[idx],
                prepared[idx],
            )

        time.sleep(0.05)

        if cycle in (0, 9, 19, 29):
            print(
                f"  serial warmup {cycle + 1}/30",
                flush=True,
            )

    print(
        "serial seed complete: "
        "left=red centre=green right=blue",
        flush=True,
    )


def async_triplet(
    context,
    handles,
    prepared,
    frame_gap_ms,
    first_submit_stagger_ms,
):
    #
    # Exactly one asynchronous OUT transfer object per physical panel.
    #
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

                # Re-arm THIS SAME transfer immediately with the
                # next 4096-byte chunk for THIS SAME physical panel.
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

    #
    # Windows steady state did not put all three first OUTs on precisely
    # the same instant. Preserve a small phase offset.
    #
    submit_times = []

    for idx, transfer in enumerate(transfers):
        submit_times.append(time.perf_counter())
        transfer.submit()

        if idx != len(transfers) - 1:
            time.sleep(first_submit_stagger_ms / 1000.0)

    #
    # One libusb event loop drives all three devices.
    #
    while True:
        terminal = [
            write_done[idx] is not None
            or write_error[idx] is not None
            for idx in range(3)
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

    #
    # Three-panel framebuffer barrier, then controller quiet period.
    #
    remaining = (
        writes_complete
        + frame_gap_ms / 1000.0
        - time.perf_counter()
    )

    if remaining > 0:
        time.sleep(remaining)

    #
    # STATUS PHASE
    #
    # Crucial difference from our Python-thread experiment:
    # all three IN URBs are submitted BEFORE we process completion events.
    #
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

    # Submit ALL THREE before calling handleEvents().
    for transfer in read_transfers:
        read_submit_times.append(time.perf_counter())
        transfer.submit()

    while True:
        terminal = [
            read_done[idx] is not None
            or read_error[idx] is not None
            for idx in range(3)
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

    write_ms = (
        writes_complete - frame_started
    ) * 1000.0

    gap_actual_ms = (
        min(read_submit_times) - writes_complete
    ) * 1000.0

    status_ms = (
        max(read_done) - min(read_submit_times)
    ) * 1000.0

    total_ms = (
        frame_finished - frame_started
    ) * 1000.0

    first_submit_span_ms = (
        max(submit_times) - min(submit_times)
    ) * 1000.0

    read_submit_span_ms = (
        max(read_submit_times) - min(read_submit_times)
    ) * 1000.0

    print()
    print("ASYNC TRIPLET COMPLETE")
    print(
        f"  write phase:       {write_ms:.3f} ms"
    )
    print(
        f"  first-submit span: {first_submit_span_ms:.3f} ms"
    )
    print(
        f"  frame/status gap:  {gap_actual_ms:.3f} ms"
    )
    print(
        f"  status phase:      {status_ms:.3f} ms"
    )
    print(
        f"  status-submit span:{read_submit_span_ms:.3f} ms"
    )
    print(
        f"  total:             {total_ms:.3f} ms"
    )
    print(
        f"  equivalent fps:    {1000.0 / total_ms:.2f}"
    )
    print(
        "  statuses:          "
        + "/".join(
            f"0x{x:02x}"
            for x in statuses
        )
    )
    print(
        f"  commits:           {commits}"
    )

    return statuses


def hardware_run(args):
    if os.path.exists(fanlib.SOCK_PATH):
        raise RuntimeError(
            f"{fanlib.SOCK_PATH} exists. "
            "Stop jonsbo-fan-daemon.service before --run."
        )

    context = usb1.USBContext()
    handles = []

    try:
        found = discover_panels(context)

        print("found panels:")
        for idx, item in enumerate(found):
            serial, bus, address, _ = item
            print(
                f"  {idx}: {serial or '?'} "
                f"bus={bus} address={address}"
            )

        handles = claim_all(found)

        # Same safe ordering as the existing daemon:
        # claim all -> init all.
        init_all(handles)

        # Establish a known-clean framebuffer using only the
        # already-proven serial behaviour.
        serial_seed(handles)

        print()
        print("Before the async test:")
        print("  LEFT   should be solid red")
        print("  CENTRE should be solid green")
        print("  RIGHT  should be solid blue")
        print()
        print(
            "The next action sends EXACTLY ONE "
            "three-panel async gray frame."
        )
        print(
            "If those three seed panels are not perfectly solid, "
            "press Ctrl+C instead."
        )
        print()

        input(
            "Press Enter to send ONE async triplet, "
            "or Ctrl+C to abort: "
        )

        gray = (70, 70, 70)

        prepared = [
            prepare_image(gray),
            prepare_image(gray),
            prepare_image(gray),
        ]

        async_triplet(
            context,
            handles,
            prepared,
            frame_gap_ms=args.frame_gap_ms,
            first_submit_stagger_ms=args.first_submit_stagger_ms,
        )

        print()
        print(
            "TEST FINISHED. Do not send another frame yet."
        )
        print(
            "Inspect all three displays for any surviving "
            "red/green/blue region."
        )
        print()
        input(
            "Leave the panels exactly as they are and inspect them. "
            "Press Enter only when ready to release the USB interfaces: "
        )

    finally:
        for handle in handles:
            try:
                handle.releaseInterface(0)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Jonsbo ZC-360 true-libusb-async "
            "three-panel probe"
        )
    )

    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "actually claim the panels and perform "
            "one controlled async hardware test"
        ),
    )

    parser.add_argument(
        "--frame-gap-ms",
        type=float,
        default=DEFAULT_FRAME_GAP_MS,
    )

    parser.add_argument(
        "--first-submit-stagger-ms",
        type=float,
        default=DEFAULT_FIRST_SUBMIT_STAGGER_MS,
    )

    args = parser.parse_args()

    if fanlib.FRAME_RECORDS_PER_WRITE != 8:
        raise RuntimeError(
            "probe requires "
            "JONSBO_FRAME_RECORDS_PER_WRITE=8"
        )

    if not args.run:
        dry_run()
        return

    hardware_run(args)


if __name__ == "__main__":
    main()

"""Single-owner ZC-360 USB daemon and local client server."""

from __future__ import annotations

import os
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import __version__
from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH, PANEL_COUNT, PRODUCT_ID, VENDOR_ID, socket_path
from .ipc import (
    IPC_VERSION,
    OP_FRAME_SET,
    OP_HELLO,
    STATUS_BAD_REQUEST,
    STATUS_OK,
    STATUS_PROCESSING_ERROR,
    STATUS_UNSUPPORTED_VERSION,
    IPCError,
    decode_frame_packet,
    encode_response,
    receive_legacy_frames,
    receive_request,
)
from .lifecycle import install_shutdown_handler

WARMUP_CYCLES = 30
WARMUP_DELAY = 0.05


@dataclass
class DriverState:
    labels: list[str]
    warmed: list[bool]
    started_monotonic: float = field(default_factory=time.monotonic)
    frame_sets: int = 0
    processing_errors: int = 0
    last_transfer: dict[str, Any] | None = None
    perf_started: float = field(default_factory=time.monotonic)
    perf_frames: int = 0
    perf_total_ms: float = 0.0
    perf_min_ms: float | None = None
    perf_max_ms: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "driver": "jonsbo-zc360",
            "driver_version": __version__,
            "ipc_version": IPC_VERSION,
            "panels": [
                {"index": index, "serial": label, "warmed": self.warmed[index]}
                for index, label in enumerate(self.labels)
            ],
            "frame_sets": self.frame_sets,
            "processing_errors": self.processing_errors,
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 3),
            "last_transfer": self.last_transfer,
            "usb_owner_policy": "single-claim-fail-closed",
        }

    def record_transfer(self, result: dict[str, Any]) -> None:
        """Keep exact last-frame status while rate-limiting journal output."""
        self.frame_sets += 1
        self.last_transfer = result
        packet_ms = float(result["packet_ms"])

        if self.frame_sets == 1:
            print(
                f"USB PERF first packet={packet_ms:.2f}ms "
                f"panels={len(result['panels'])} mode={result['mode']} "
                f"status={_format_statuses(result['statuses'])}",
                flush=True,
            )
            self.perf_started = time.monotonic()
            return

        self.perf_frames += 1
        self.perf_total_ms += packet_ms
        self.perf_min_ms = packet_ms if self.perf_min_ms is None else min(self.perf_min_ms, packet_ms)
        self.perf_max_ms = max(self.perf_max_ms, packet_ms)

        now = time.monotonic()
        interval = max(1.0, float(os.environ.get("JONSBO_PERF_LOG_INTERVAL", "5.0")))
        elapsed = now - self.perf_started
        if elapsed < interval:
            return

        print(
            f"USB PERF summary actual={self.perf_frames / elapsed:.2f}fps "
            f"avg={self.perf_total_ms / self.perf_frames:.2f}ms "
            f"min={self.perf_min_ms:.2f}ms max={self.perf_max_ms:.2f}ms "
            f"mode={result['mode']} status={_format_statuses(result['statuses'])}",
            flush=True,
        )
        self.perf_started = now
        self.perf_frames = 0
        self.perf_total_ms = 0.0
        self.perf_min_ms = None
        self.perf_max_ms = 0.0


def _format_statuses(statuses) -> str:
    if statuses is None:
        return "none"
    return "/".join("none" if status is None else f"0x{status:02x}" for status in statuses)


def startup_frame(index: int, label: str) -> Image.Image:
    image = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), (9, 12, 17))
    draw = ImageDraw.Draw(image)
    accent = ((255, 96, 180), (94, 226, 255), (255, 210, 84))[index % PANEL_COUNT]
    draw.rectangle((0, 0, LOGICAL_WIDTH - 1, LOGICAL_HEIGHT - 1), outline=accent, width=8)
    draw.rectangle((24, 26, 34, LOGICAL_HEIGHT - 27), fill=accent)
    draw.text((56, 47), f"ZC-360  PANEL {index}", fill=(235, 239, 244))
    draw.text((56, 88), label, fill=accent)
    draw.text((56, 127), "USB OWNER ONLINE", fill=(150, 158, 171))
    return image


def wait_for_usb_access(context, timeout: float = 15.0, interval: float = 0.25) -> None:
    """Wait for udev permissions without opening or claiming an interface."""
    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        devices = [
            device
            for device in context.getDeviceIterator(skip_on_error=True)
            if device.getVendorID() == VENDOR_ID and device.getProductID() == PRODUCT_ID
        ]
        paths: list[str] = []
        ready = len(devices) == PANEL_COUNT
        if ready:
            for device in devices:
                path = f"/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}"
                paths.append(path)
                if not os.access(path, os.R_OK | os.W_OK):
                    ready = False
        status = len(devices), tuple(sorted(paths)), ready
        if status != last_status:
            print(
                f"USB startup gate: found={len(devices)}/{PANEL_COUNT} "
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
            raise RuntimeError("timed out waiting for all three ZC-360 devices to become accessible")
        time.sleep(interval)


def warmup_subset(transport, handles, frames: dict[int, Image.Image]) -> None:
    prepared = {
        index: transport.prepare_image_chunks(image)
        for index, image in frames.items()
        if index < len(handles)
    }
    for _ in range(WARMUP_CYCLES):
        for index in sorted(prepared):
            transport.push_chunks_sync(handles[index], prepared[index])
        time.sleep(WARMUP_DELAY)


def process_frames(transport, context, handles, frames, warmed) -> dict[str, Any]:
    """Complete one frame set on a safe transport barrier and return metrics."""
    started = time.perf_counter()
    valid = {index: image for index, image in frames.items() if 0 <= index < len(handles)}
    if not valid:
        raise IPCError("frame set contains no valid panel indices")

    if any(not warmed[index] for index in valid):
        warmup_subset(transport, handles, valid)
        for index in valid:
            warmed[index] = True
        mode = "serial-warmup"
        statuses = None
        usb_ms = None
    elif len(valid) == PANEL_COUNT and all(index in valid for index in range(PANEL_COUNT)):
        prepared = transport.prepare_triplet([valid[0], valid[1], valid[2]])
        transfer = transport.async_triplet(
            context,
            handles,
            prepared,
            frame_gap_ms=float(
                os.environ.get("JONSBO_ASYNC_FRAME_GAP_MS", str(transport.DEFAULT_FRAME_GAP_MS))
            ),
            first_submit_stagger_ms=float(
                os.environ.get(
                    "JONSBO_ASYNC_FIRST_SUBMIT_STAGGER_MS",
                    str(transport.DEFAULT_FIRST_SUBMIT_STAGGER_MS),
                )
            ),
            report=os.environ.get("JONSBO_ASYNC_REPORT", "0") == "1",
        )
        statuses = transfer["statuses"]
        usb_ms = transfer["total_ms"]
        mode = "async-triplet"
    else:
        panel_results = []
        statuses = []
        for index in sorted(valid):
            panel_started = time.perf_counter()
            status = transport.push_image_sync(handles[index], valid[index])
            elapsed = (time.perf_counter() - panel_started) * 1000.0
            statuses.append(status)
            panel_results.append({"index": index, "milliseconds": round(elapsed, 3), "status": status})
        mode = "serial-partial"
        usb_ms = sum(result["milliseconds"] for result in panel_results)

    packet_ms = (time.perf_counter() - started) * 1000.0
    result = {
        "mode": mode,
        "panels": sorted(valid),
        "packet_ms": round(packet_ms, 3),
        "usb_ms": None if usb_ms is None else round(usb_ms, 3),
        "statuses": statuses,
    }
    return result


def remove_stale_socket(path: Path) -> None:
    """Remove only an unowned socket; never displace a live USB owner."""
    if not path.exists():
        return
    if not stat.S_ISSOCK(path.lstat().st_mode):
        raise RuntimeError(f"refusing to replace non-socket path: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(str(path))
    except OSError:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    finally:
        probe.close()
    raise RuntimeError(f"{path} already has a live listener; refusing to start a second USB owner")


def _send_versioned(connection: socket.socket, status: int, payload: dict[str, Any]) -> None:
    try:
        connection.sendall(encode_response(status, payload))
    except OSError:
        pass


def serve_connection(connection, transport, context, handles, state: DriverState) -> None:
    first = connection.recv(1)
    if not first:
        return

    if first[0] != 0:
        try:
            frames = receive_legacy_frames(connection, first[0])
            result = process_frames(transport, context, handles, frames, state.warmed)
            state.record_transfer(result)
            connection.sendall(b"\x01")
        except Exception as exc:
            state.processing_errors += 1
            print(f"Legacy request error: {exc}", flush=True)
            try:
                connection.sendall(b"\x00")
            except OSError:
                pass
        return

    try:
        request = receive_request(connection, first[0])
    except Exception as exc:
        _send_versioned(connection, STATUS_BAD_REQUEST, {"error": str(exc)})
        return
    if request.version != IPC_VERSION:
        _send_versioned(
            connection,
            STATUS_UNSUPPORTED_VERSION,
            {"error": f"unsupported IPC version {request.version}", "supported": IPC_VERSION},
        )
        return
    if request.opcode == OP_HELLO:
        _send_versioned(connection, STATUS_OK, state.snapshot())
        return
    if request.opcode != OP_FRAME_SET:
        _send_versioned(connection, STATUS_BAD_REQUEST, {"error": f"unknown opcode {request.opcode}"})
        return

    try:
        frames = decode_frame_packet(request.payload)
        result = process_frames(transport, context, handles, frames, state.warmed)
        state.record_transfer(result)
        _send_versioned(connection, STATUS_OK, result)
    except Exception as exc:
        state.processing_errors += 1
        print(f"Versioned request error: {exc}", flush=True)
        _send_versioned(connection, STATUS_PROCESSING_ERROR, {"error": str(exc)})


def main() -> None:
    # Imports are delayed so protocol, CLI help, and offline tests never load
    # a USB backend merely by importing the package.
    import usb1

    from . import transport

    path = socket_path()
    print(
        "Jonsbo ZC-360 USB owner: persistent libusb1, three-panel fail-closed policy",
        flush=True,
    )
    context = usb1.USBContext()
    handles = []
    server = None
    shutdown_requested = install_shutdown_handler()

    try:
        remove_stale_socket(path)
        wait_for_usb_access(context)
        found = transport.discover_panels(context)
        labels = [item[0] or f"panel-{index}" for index, item in enumerate(found)]
        print("found panels:", flush=True)
        for index, item in enumerate(found):
            _serial, bus, address, _device = item
            print(f"  {index}: {labels[index]} bus={bus} address={address}", flush=True)

        handles = transport.claim_all(found)
        transport.init_all(handles)
        transport.warmup_interleaved(
            handles,
            [startup_frame(index, label) for index, label in enumerate(labels)],
            n=WARMUP_CYCLES,
            delay=WARMUP_DELAY,
        )
        state = DriverState(labels=labels, warmed=[True] * len(handles))
        print("All three panels initialized and warmed on persistent libusb1 handles.", flush=True)

        path.parent.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(4)
        server.settimeout(1.0)
        print(f"Socket ready: {path} (IPC v{IPC_VERSION} + legacy frames)", flush=True)

        while not shutdown_requested.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(5.0)
                serve_connection(connection, transport, context, handles, state)
    finally:
        print("Shutting down at a completed frame boundary; no async transfer is in flight.", flush=True)
        if server is not None:
            server.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        transport.release_all(handles)
        try:
            context.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Jonsbo TF3 / ZC-360 fan-panel USB daemon - keeps the 3 fan-panel USB interfaces
claimed permanently and accepts already-rendered images over a Unix socket
(see jonsbo_fan_lib.send_fan_frames()).

Why USB claiming and rendering are split into two processes (see README.md):
every claim/release cycle on the 3 fan panels risks wedging them (panels go
black, the command/ACK layer stays responsive, only a physical power cycle
recovers it). As long as USB claiming and rendering lived in the same
process, every code deploy (systemctl restart) also re-claimed the USB
connection and reliably wedged the panels. This daemon is meant to be
started once and never restarted again after that - rendering/logic changes
go through the separate, safely-restartable jonsbo_monitor.py client, which
never touches USB directly.
"""
import io
import os
import socket
import struct
import sys
import time
import threading
from vendor_triplet import process_vendor_triplet
import usb.core

from PIL import Image, ImageDraw

from jonsbo_fan_lib import (
    LOGICAL_H,
    LOGICAL_W,
    SOCK_PATH,
    find_panels,
    install_sigterm_handler,
)

STALE_AFTER = 30  # s without a new frame for a panel -> next frame gets a full warmup


def startup_frame(index, panel):
    """Visible ownership marker used for the first interleaved warmup."""
    img = Image.new("RGB", (LOGICAL_W, LOGICAL_H), (9, 12, 17))
    draw = ImageDraw.Draw(img)
    accent = ((255, 96, 180), (94, 226, 255), (255, 210, 84))[index % 3]
    draw.rectangle((0, 0, LOGICAL_W - 1, LOGICAL_H - 1), outline=accent, width=8)
    draw.rectangle((24, 26, 34, LOGICAL_H - 27), fill=accent)
    draw.text((56, 47), f"ZC-360  PANEL {index}", fill=(235, 239, 244))
    draw.text((56, 88), panel.label, fill=accent)
    draw.text((56, 127), "USB OWNER ONLINE", fill=(150, 158, 171))
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
    """Reads one frame packet: 1 byte count, then per panel (1 byte idx,
    4 byte big-endian length, PNG bytes). Returns dict idx->PIL.Image, or
    None on a dropped connection/malformed packet."""
    head = recv_exact(conn, 1)
    if head is None:
        return None
    count = head[0]
    out = {}
    for _ in range(count):
        h = recv_exact(conn, 5)
        if h is None:
            return None
        idx, length = struct.unpack(">BI", h)
        data = recv_exact(conn, length)
        if data is None:
            return None
        out[idx] = Image.open(io.BytesIO(data)).convert("RGB")
    return out


def process_frames(panels, frames, warmed, last_seen):
    """If ANY panel in this packet is fresh (never warmed up) or hasn't been
    fed in STALE_AFTER seconds, all panels included in this packet get
    pushed 30x interleaved - a claimed-but-unfed panel otherwise goes black
    while its neighbors are being pushed frames, so all panels a client
    cares about need to be warmed up together, in lockstep."""
    now = time.time()
    needs_warmup = any(
        idx < len(panels) and (not warmed[idx] or (now - last_seen[idx]) > STALE_AFTER)
        for idx in frames
    )
    if needs_warmup:
        for _ in range(30):
            for idx, img in frames.items():
                if idx >= len(panels):
                    continue
                try:
                    panels[idx].push_image(img)
                except Exception as e:
                    print(f"[{panels[idx].label}] warmup error: {e}")
            time.sleep(0.05)
        for idx in frames:
            if idx < len(panels):
                warmed[idx] = True
    elif (
        os.environ.get("JONSBO_VENDOR_TRIPLET", "0") == "1"
        and len(panels) == 3
        and all(idx in frames for idx in range(3))
    ):
        process_vendor_triplet(
            panels,
            frames,
        )
    else:
        packet_started = time.perf_counter()

        valid_frames = [
            (idx, img)
            for idx, img in frames.items()
            if idx < len(panels)
        ]

        stagger_ms = float(
            os.environ.get("JONSBO_TWO_PANEL_STAGGER_MS", "0")
        )

        def push_one(idx, img, delay_seconds=0.0):
            if delay_seconds > 0:
                time.sleep(delay_seconds)

            started = time.perf_counter()
            error = None

            try:
                panels[idx].push_image(img)
            except Exception as e:
                error = e

            elapsed = (time.perf_counter() - started) * 1000.0
            return idx, elapsed, error

        if stagger_ms > 0 and len(valid_frames) >= 2:
            results = [None] * len(valid_frames)

            def worker(position, item):
                idx, img = item
                results[position] = push_one(
                    idx,
                    img,
                    position * stagger_ms / 1000.0,
                )

            threads = [
                threading.Thread(
                    target=worker,
                    args=(position, item),
                )
                for position, item in enumerate(valid_frames)
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

        else:
            results = [
                push_one(idx, img)
                for idx, img in valid_frames
            ]

        for idx, elapsed, error in results:
            if error is not None:
                print(f"[{panels[idx].label}] push error: {error}")

            print(
                f"USB PERF panel={idx} "
                f"push={elapsed:.2f}ms",
                flush=True,
            )

        total = (time.perf_counter() - packet_started) * 1000.0

        print(
            f"USB PERF packet={total:.2f}ms "
            f"panels={len(valid_frames)} "
            f"stagger={stagger_ms:.1f}ms",
            flush=True,
        )
    for idx in frames:
        if idx < len(panels):
            last_seen[idx] = now



def wait_for_usb_access(timeout=15.0, interval=0.25):
    """Wait for udev/session permissions before touching the panel descriptors.

    This deliberately does NOT claim or release interfaces while waiting.
    Once all three device nodes are accessible, normal discovery/claiming
    proceeds exactly once.
    """
    vid = 0x43A8
    pid = 0x0E61
    deadline = time.monotonic() + timeout
    last_status = None

    while True:
        devs = list(
            usb.core.find(
                find_all=True,
                idVendor=vid,
                idProduct=pid,
            )
        )

        paths = []
        ready = len(devs) == 3

        if ready:
            for dev in devs:
                bus = getattr(dev, "bus", None)
                address = getattr(dev, "address", None)

                if bus is None or address is None:
                    ready = False
                    continue

                device_path = f"/dev/bus/usb/{bus:03d}/{address:03d}"
                paths.append(device_path)

                if not os.access(device_path, os.R_OK | os.W_OK):
                    ready = False

        status = (
            len(devs),
            tuple(paths),
            ready,
        )

        if status != last_status:
            print(
                f"USB startup gate: found={len(devs)}/3 "
                f"accessible={'yes' if ready else 'no'}",
                flush=True,
            )
            last_status = status

        if ready:
            print(
                "USB startup gate: all three panels are accessible; "
                "continuing with one claim cycle.",
                flush=True,
            )
            return

        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Timed out waiting for all three ZC-360 USB devices "
                "to become accessible"
            )

        time.sleep(interval)


class ContinuousFrameScheduler:
    """Latest-frame-wins round-robin scheduler.

    Starts one panel transfer every interval_ms while allowing at most
    max_inflight transfers at once. A panel is never written concurrently
    with itself.

    Shutdown waits for every in-flight push_image() to finish before USB
    interfaces may be released.
    """

    def __init__(self, panels, interval_ms=20.0, max_inflight=2):
        self.panels = panels
        self.interval_s = interval_ms / 1000.0
        self.interval_ms = interval_ms
        self.max_inflight = max_inflight

        self.stop_event = threading.Event()
        self.cv = threading.Condition()

        count = len(panels)

        self.latest = {}
        self.generation = [0] * count
        self.sent_generation = [0] * count
        self.busy = [False] * count

        self.active = 0
        self.workers = set()
        self.thread = None

        self.input_count = [0] * count
        self.output_count = [0] * count
        self.push_seconds = [0.0] * count
        self.stats_started = time.monotonic()

    def start(self):
        self.thread = threading.Thread(
            target=self._run,
            name="zc360-continuous-scheduler",
            daemon=False,
        )
        self.thread.start()

        print(
            f"Continuous scheduler: interval={self.interval_ms:.1f}ms "
            f"max_inflight={self.max_inflight} latest-frame-wins",
            flush=True,
        )

    def submit(self, frames):
        """Replace each panel's pending image with the newest one."""
        with self.cv:
            for idx, img in frames.items():
                if idx >= len(self.panels):
                    continue

                self.latest[idx] = img
                self.generation[idx] += 1
                self.input_count[idx] += 1

            self.cv.notify_all()

    def _push_worker(self, idx, img, generation):
        started = time.perf_counter()
        error = None

        try:
            self.panels[idx].push_image(img)
        except Exception as exc:
            error = exc

        elapsed = time.perf_counter() - started

        with self.cv:
            self.busy[idx] = False
            self.active -= 1

            if error is None:
                self.sent_generation[idx] = max(
                    self.sent_generation[idx],
                    generation,
                )
                self.output_count[idx] += 1
                self.push_seconds[idx] += elapsed

            self.workers.discard(threading.current_thread())

            self._maybe_report_locked()
            self.cv.notify_all()

        if error is not None:
            print(
                f"[{self.panels[idx].label}] scheduler push error: {error}",
                flush=True,
            )

    def _maybe_report_locked(self):
        now = time.monotonic()
        elapsed = now - self.stats_started

        if elapsed < 5.0:
            return

        input_fps = [
            count / elapsed
            for count in self.input_count
        ]

        output_fps = [
            count / elapsed
            for count in self.output_count
        ]

        avg_push_ms = [
            (
                self.push_seconds[idx] * 1000.0
                / self.output_count[idx]
            )
            if self.output_count[idx]
            else 0.0
            for idx in range(len(self.panels))
        ]

        print(
            "SCHED PERF "
            f"in={'/'.join(f'{v:.1f}' for v in input_fps)}fps "
            f"out={'/'.join(f'{v:.1f}' for v in output_fps)}fps "
            f"push={'/'.join(f'{v:.1f}' for v in avg_push_ms)}ms "
            f"interval={self.interval_ms:.1f}ms "
            f"max_inflight={self.max_inflight}",
            flush=True,
        )

        self.input_count = [0] * len(self.panels)
        self.output_count = [0] * len(self.panels)
        self.push_seconds = [0.0] * len(self.panels)
        self.stats_started = now

    def _run(self):
        idx = 0
        next_launch = time.monotonic()

        while not self.stop_event.is_set():
            delay = next_launch - time.monotonic()

            if delay > 0:
                if self.stop_event.wait(delay):
                    break

            with self.cv:
                # Never permit a third transfer, and never write the same
                # physical panel twice concurrently.
                while (
                    not self.stop_event.is_set()
                    and (
                        self.active >= self.max_inflight
                        or self.busy[idx]
                    )
                ):
                    self.cv.wait(timeout=0.005)

                if self.stop_event.is_set():
                    break

                generation = self.generation[idx]
                img = self.latest.get(idx)

                should_send = (
                    img is not None
                    and generation > self.sent_generation[idx]
                )

                if should_send:
                    self.busy[idx] = True
                    self.active += 1

                    worker = threading.Thread(
                        target=self._push_worker,
                        args=(idx, img, generation),
                        name=f"zc360-panel-{idx}",
                        daemon=False,
                    )

                    self.workers.add(worker)
                else:
                    worker = None

            if worker is not None:
                worker.start()

            actual = time.monotonic()

            idx = (idx + 1) % len(self.panels)

            # Do not "catch up" by launching several transfers together if
            # one transfer ran slightly late. Preserve the minimum spacing.
            next_launch = max(
                next_launch + self.interval_s,
                actual + self.interval_s,
            )

    def stop(self):
        print(
            "Stopping continuous scheduler; draining USB transfers...",
            flush=True,
        )

        self.stop_event.set()

        with self.cv:
            self.cv.notify_all()

        if self.thread is not None:
            self.thread.join()

        while True:
            with self.cv:
                workers = list(self.workers)

            if not workers:
                break

            for worker in workers:
                worker.join()

        print(
            "Continuous scheduler drained; no USB transfers in flight.",
            flush=True,
        )

def main():
    print("Jonsbo ZC-360 USB owner - keeps 3 panels claimed, Ctrl+C to stop")
    wait_for_usb_access()
    panels = find_panels()
    if len(panels) != 3:
        print(f"Expected exactly 3 panels, found {len(panels)} - exiting before claim.")
        sys.exit(1)
    for index, panel in enumerate(panels):
        print(f"Panel {index}: {panel.label}")

    shutdown_requested = install_sigterm_handler()

    # Claim ALL panels first, THEN send init commands on all of them - see
    # FanPanel.open() docstring.
    for p in panels:
        p.open(claim_only=True, settle=0.3)
    for p in panels:
        p.init_commands()

    warmed = [False] * len(panels)
    last_seen = [0.0] * len(panels)
    process_frames(
        panels,
        {index: startup_frame(index, panel) for index, panel in enumerate(panels)},
        warmed,
        last_seen,
    )
    print("All three panels initialized and warmed in lockstep.")

    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(1)
    srv.settimeout(1.0)
    print(f"Socket ready: {SOCK_PATH}")

    continuous_scheduler = None

    if os.environ.get("JONSBO_CONTINUOUS_SCHEDULER", "0") == "1":
        interval_ms = float(
            os.environ.get("JONSBO_SCHEDULER_INTERVAL_MS", "20")
        )
        max_inflight = int(
            os.environ.get("JONSBO_SCHEDULER_MAX_INFLIGHT", "2")
        )

        continuous_scheduler = ContinuousFrameScheduler(
            panels,
            interval_ms=interval_ms,
            max_inflight=max_inflight,
        )
        continuous_scheduler.start()

    try:
        while not shutdown_requested.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    frames = recv_frames(conn)
                except Exception as e:
                    print(f"Receive error: {e}")
                    continue
                if not frames:
                    continue
                try:
                    if continuous_scheduler is not None:
                        continuous_scheduler.submit(frames)
                    else:
                        process_frames(
                            panels,
                            frames,
                            warmed,
                            last_seen,
                        )
                except Exception as e:
                    print(f"Error while processing: {e}")
                try:
                    conn.sendall(b"\x01")
                except OSError:
                    pass
    finally:
        if continuous_scheduler is not None:
            continuous_scheduler.stop()

        print("Shutting down cleanly (no transfers in flight), closing panels...")
        for p in panels:
            p.close()
        try:
            srv.close()
        except OSError:
            pass
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)


if __name__ == "__main__":
    main()

"""Generic socket-only media and dashboard renderer for the ZC-360 daemon."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from PIL import Image

from . import __version__
from .cli import test_pattern
from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH
from .dashboards import ExternalFrameSource, SystemDashboard, WeatherDashboard
from .ipc import encode_fan_frames, send_fan_packet
from .media import PanelSourceSet, join_triptych, open_source, split_frame
from .state import read_state, state_path


class LatestFrameSender:
    """Keep one socket request in flight and one replaceable pending frame."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self.condition = threading.Condition()
        self.pending: bytes | None = None
        self.stopping = False
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, name="zc360-frame-sender", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit(self, images: dict[int, Image.Image]) -> None:
        packet, _ = encode_fan_frames(images)
        with self.condition:
            self.pending = packet
            self.condition.notify()

    def _run(self) -> None:
        while True:
            with self.condition:
                while self.pending is None and not self.stopping:
                    self.condition.wait()
                if self.stopping:
                    return
                packet = self.pending
                self.pending = None
            try:
                send_fan_packet(packet, timeout=self.timeout)
                if self.error is not None:
                    print("Renderer reconnected to the USB owner", flush=True)
                self.error = None
            except Exception as exc:
                message = str(exc)
                if message != self.error:
                    print(f"Renderer waiting for USB owner: {message}", flush=True)
                self.error = message

    def stop(self) -> None:
        with self.condition:
            self.stopping = True
            self.pending = None
            self.condition.notify_all()
        self.thread.join(timeout=2.0)


def source_key(state: dict) -> tuple:
    return (
        state.get("mode"),
        state.get("source", ""),
        tuple(state.get("sources", [])),
        state.get("layout"),
        state.get("fit"),
        json.dumps(state.get("framing", {}), sort_keys=True),
        json.dumps(state.get("panel_framing", []), sort_keys=True),
        tuple(state.get("panel_order", [0, 1, 2])),
        state.get("frames_per_second"),
    )


def open_state_source(state: dict):
    if state["layout"] == "panels":
        return PanelSourceSet(
            [Path(item) for item in state.get("sources", [])],
            state["fit"],
            state["frames_per_second"],
            state.get("panel_framing"),
        )
    return open_source(
        Path(state.get("source", "")),
        state["layout"],
        state["fit"],
        state["frames_per_second"],
        state.get("framing"),
    )


def source_images(source, state: dict) -> tuple[dict[int, Image.Image], float]:
    frame, delay = source.frame()
    order = state["panel_order"]
    if state["layout"] == "panels":
        return {index: frame[column] for column, index in enumerate(order)}, delay
    return split_frame(frame, state["layout"], order), delay


def dashboard_key(state: dict) -> tuple:
    mode = state.get("mode")
    common = (mode, tuple(state.get("panel_order", [0, 1, 2])))
    if mode == "telemetry":
        return common + (state.get("telemetry_refresh_seconds"),)
    if mode == "weather":
        return common + (
            state.get("weather_location", ""),
            state.get("weather_units", "metric"),
            state.get("weather_refresh_seconds"),
        )
    if mode == "external":
        return common + (
            state.get("external_directory", ""),
            state.get("external_refresh_seconds"),
        )
    return common


def open_dashboard_source(state: dict):
    if state["mode"] == "telemetry":
        return SystemDashboard(state)
    if state["mode"] == "weather":
        return WeatherDashboard(state)
    if state["mode"] == "external":
        return ExternalFrameSource(state)
    raise RuntimeError(f"unsupported dashboard mode: {state['mode']}")


def identify_active(state: dict, now: float | None = None) -> bool:
    try:
        until = float(state.get("identify_until", 0.0))
    except (TypeError, ValueError):
        return False
    return until > (time.time() if now is None else now)


def preview(state: dict, directory: Path) -> None:
    source = open_state_source(state)
    try:
        images, _ = source_images(source, state)
    finally:
        source.close()
    directory.mkdir(parents=True, exist_ok=True)
    join_triptych(images, state["panel_order"]).save(directory / "triptych.png")
    for index, image in images.items():
        image.save(directory / f"panel-{index}.png")
    print(f"Preview written to {directory}; no socket or USB access occurred.")


def run() -> None:
    sender = LatestFrameSender()
    sender.start()
    current_source = None
    current_key = None
    submitted_static = False
    static_retry_after = 0.0
    blank_key = None
    blank_retry_after = 0.0
    identify_key = None
    dashboard_source = None
    current_dashboard_key = None
    next_dashboard_frame = 0.0
    logged_error = None
    print(f"ZC-360 renderer {__version__}; state: {state_path()}", flush=True)
    try:
        while True:
            state = read_state()
            if identify_active(state):
                if current_source:
                    current_source.close()
                    current_source = None
                if dashboard_source:
                    dashboard_source.close()
                    dashboard_source = None
                current_key = None
                current_dashboard_key = None
                try:
                    key = float(state["identify_until"])
                except (TypeError, ValueError, KeyError):
                    key = time.time() + 0.1
                now = time.monotonic()
                if identify_key != key or (sender.error is not None and now >= static_retry_after):
                    sender.submit({index: test_pattern(index) for index in range(3)})
                    identify_key = key
                    static_retry_after = now + 2.0
                time.sleep(0.1)
                continue
            identify_key = None

            if state.get("paused") or state.get("framing_editing"):
                if current_source:
                    current_source.close()
                    current_source = None
                if dashboard_source:
                    dashboard_source.close()
                    dashboard_source = None
                current_key = None
                current_dashboard_key = None
                time.sleep(0.1)
                continue

            if state.get("mode") == "blank":
                key = tuple(state["panel_order"])
                now = time.monotonic()
                if blank_key != key or (sender.error is not None and now >= blank_retry_after):
                    black = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), (0, 0, 0))
                    sender.submit({index: black for index in state["panel_order"]})
                    blank_key = key
                    blank_retry_after = now + 2.0
                time.sleep(0.2)
                continue
            blank_key = None

            if state.get("mode") in {"telemetry", "weather", "external"}:
                if current_source:
                    current_source.close()
                    current_source = None
                current_key = None
                key = dashboard_key(state)
                if key != current_dashboard_key:
                    if dashboard_source:
                        dashboard_source.close()
                    try:
                        dashboard_source = open_dashboard_source(state)
                        current_dashboard_key = key
                        next_dashboard_frame = 0.0
                        logged_error = None
                        print(f"Display mode loaded: {state['mode']}", flush=True)
                    except Exception as exc:
                        dashboard_source = None
                        current_dashboard_key = None
                        message = str(exc)
                        if message != logged_error:
                            print(f"Display mode unavailable: {message}", flush=True)
                        logged_error = message
                        time.sleep(1.0)
                        continue
                now = time.monotonic()
                if now < next_dashboard_frame:
                    time.sleep(min(0.2, next_dashboard_frame - now))
                    continue
                try:
                    images, delay = dashboard_source.frame()
                    sender.submit(images)
                    next_dashboard_frame = time.monotonic() + delay
                    logged_error = None
                except Exception as exc:
                    message = str(exc)
                    if message != logged_error:
                        print(f"Display mode update failed: {message}", flush=True)
                    logged_error = message
                    next_dashboard_frame = time.monotonic() + 2.0
                continue
            if dashboard_source:
                dashboard_source.close()
                dashboard_source = None
            current_dashboard_key = None

            if state.get("mode") != "media":
                if current_source:
                    current_source.close()
                    current_source = None
                current_key = None
                time.sleep(0.2)
                continue

            key = source_key(state)
            if key != current_key:
                if current_source:
                    current_source.close()
                try:
                    current_source = open_state_source(state)
                    current_key = key
                    submitted_static = False
                    static_retry_after = 0.0
                    logged_error = None
                    print(f"Media loaded: {state.get('source') or state.get('sources')}", flush=True)
                except Exception as exc:
                    current_source = None
                    current_key = None
                    message = str(exc)
                    if message != logged_error:
                        print(f"Media unavailable: {message}", flush=True)
                    logged_error = message
                    time.sleep(1.0)
                    continue

            if not current_source:
                time.sleep(0.2)
                continue
            now = time.monotonic()
            if (
                not current_source.animated
                and submitted_static
                and not (sender.error is not None and now >= static_retry_after)
            ):
                time.sleep(0.2)
                continue
            started = time.monotonic()
            try:
                images, delay = source_images(current_source, state)
                sender.submit(images)
                submitted_static = True
                static_retry_after = time.monotonic() + 2.0
            except Exception as exc:
                message = str(exc)
                if message != logged_error:
                    print(f"Media decode failed: {message}", flush=True)
                logged_error = message
                current_source.close()
                current_source = None
                current_key = None
                time.sleep(1.0)
                continue
            time.sleep(max(0.0, delay - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        if current_source:
            current_source.close()
        if dashboard_source:
            dashboard_source.close()
        sender.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"zc360-renderer {__version__}")
    parser.add_argument("--preview", action="store_true", help="render current state without socket access")
    parser.add_argument("--preview-dir", type=Path, default=Path("/tmp/zc360-preview"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.preview:
        preview(read_state(), args.preview_dir)
        return 0
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

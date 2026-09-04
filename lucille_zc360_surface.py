#!/usr/bin/env python3
"""Unified media, shell-event, and diagnostic renderer for ZC-360 panels.

Only the local daemon socket is used. This process never owns USB.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from lucille_zc360_control import read_state, state_path
from lucille_zc360_renderer import (
    BG,
    MUTED,
    PALE,
    PINK,
    YELLOW,
    F_LABEL,
    F_MICRO,
    F_SMALL,
    History,
    MetricCollector,
    _demo_metrics,
    load_config,
    render_triptych,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "zc360-layout.json"
PANEL_W, PANEL_H = 640, 180
TRIPTYCH_W = PANEL_W * 3
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass
class PerformanceWindow:
    started: float = 0.0
    frames: int = 0
    decode_seconds: float = 0.0
    compose_seconds: float = 0.0
    encode_seconds: float = 0.0
    transfer_seconds: float = 0.0
    payload_bytes: int = 0

    def reset(self, now: float | None = None):
        self.started = time.monotonic() if now is None else now
        self.frames = 0
        self.decode_seconds = 0.0
        self.compose_seconds = 0.0
        self.encode_seconds = 0.0
        self.transfer_seconds = 0.0
        self.payload_bytes = 0

    def add(self, decode: float, compose: float, transport: dict):
        self.frames += 1
        self.decode_seconds += decode
        self.compose_seconds += compose
        self.encode_seconds += float(transport.get("encode_seconds", 0.0))
        self.transfer_seconds += float(transport.get("transfer_seconds", 0.0))
        self.payload_bytes += int(transport.get("payload_bytes", 0))

    def report_if_ready(self, media_active: bool):
        now = time.monotonic()
        elapsed = now - self.started
        if not media_active or self.frames == 0 or elapsed < 5.0:
            return
        count = self.frames
        print(
            "PERF "
            f"actual={count / elapsed:.2f}fps "
            f"decode={self.decode_seconds * 1000 / count:.1f}ms "
            f"compose={self.compose_seconds * 1000 / count:.1f}ms "
            f"encode={self.encode_seconds * 1000 / count:.1f}ms "
            f"socket+usb={self.transfer_seconds * 1000 / count:.1f}ms "
            f"payload={self.payload_bytes / count / 1024:.0f}KiB"
        )
        self.reset(now)


class LatestPacketSender:
    """One in-flight daemon request plus one replaceable pending packet."""

    def __init__(self, send_packet, timeout: float):
        self.send_packet = send_packet
        self.timeout = timeout
        self.cv = threading.Condition()
        self.pending = None
        self.stopping = False
        self.performance = PerformanceWindow()
        self.performance.reset()
        self.performance_lock = threading.Lock()
        self.produced = 0
        self.sent = 0
        self.replaced = 0
        self.pipe_started = time.monotonic()
        self.logged_error = None
        self.thread = threading.Thread(
            target=self._run,
            name="zc360-latest-packet-sender",
            daemon=True,
        )

    def start(self):
        self.thread.start()
        print(
            "Pipeline: latest-frame sender enabled "
            "(one packet in flight, one replaceable pending packet)"
        )

    def reset_performance(self):
        with self.performance_lock:
            self.performance.reset()
        with self.cv:
            self.produced = 0
            self.sent = 0
            self.replaced = 0
            self.pipe_started = time.monotonic()

    def submit(
        self,
        packet: bytes,
        decode_seconds: float,
        compose_seconds: float,
        encode_seconds: float,
        payload_bytes: int,
        media_active: bool,
    ):
        item = (
            packet,
            decode_seconds,
            compose_seconds,
            encode_seconds,
            payload_bytes,
            media_active,
        )
        with self.cv:
            if self.pending is not None:
                self.replaced += 1
            self.pending = item
            self.produced += 1
            self.cv.notify()

    def _report_pipeline_if_ready(self, media_active: bool):
        if not media_active:
            return
        now = time.monotonic()
        with self.cv:
            elapsed = now - self.pipe_started
            if elapsed < 5.0:
                return
            produced = self.produced
            sent = self.sent
            replaced = self.replaced
            pending = self.pending is not None
            self.produced = 0
            self.sent = 0
            self.replaced = 0
            self.pipe_started = now

        print(
            "PIPE PERF "
            f"produced={produced / elapsed:.2f}fps "
            f"sent={sent / elapsed:.2f}fps "
            f"replaced={replaced / elapsed:.2f}fps "
            f"pending={1 if pending else 0}"
        )

    def _run(self):
        while True:
            with self.cv:
                while self.pending is None and not self.stopping:
                    self.cv.wait()
                if self.stopping:
                    return
                (
                    packet,
                    decode_seconds,
                    compose_seconds,
                    encode_seconds,
                    payload_bytes,
                    media_active,
                ) = self.pending
                self.pending = None

            try:
                transport = self.send_packet(
                    packet,
                    timeout=self.timeout,
                )
                transport["encode_seconds"] = encode_seconds
                transport["payload_bytes"] = payload_bytes

                with self.performance_lock:
                    self.performance.add(
                        decode_seconds,
                        compose_seconds,
                        transport,
                    )
                    self.performance.report_if_ready(media_active)

                with self.cv:
                    self.sent += 1

                self.logged_error = None

            except Exception as exc:
                message = str(exc)
                if message != self.logged_error:
                    print(f"Surface waiting for USB owner: {message}")
                    self.logged_error = message

            self._report_pipeline_if_ready(media_active)

    def stop(self):
        with self.cv:
            self.stopping = True
            self.pending = None
            self.cv.notify_all()

        self.thread.join(timeout=2.0)

        if self.thread.is_alive():
            print(
                "Latest-frame sender is still blocked; "
                "leaving its daemon thread to exit with the renderer."
            )


def fit_image(image: Image.Image, size: tuple[int, int], mode: str) -> Image.Image:
    image = image.convert("RGB")
    if mode == "stretch":
        return image.resize(size, Image.Resampling.LANCZOS)
    scale = min(size[0] / image.width, size[1] / image.height)
    if mode == "cover":
        scale = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    if mode == "cover":
        left = max(0, (resized.width - size[0]) // 2)
        top = max(0, (resized.height - size[1]) // 2)
        return resized.crop((left, top, left + size[0], top + size[1]))
    canvas = Image.new("RGB", size, BG)
    canvas.paste(resized, ((size[0] - resized.width) // 2, (size[1] - resized.height) // 2))
    return canvas


class MediaSource:
    def frame(self) -> tuple[Image.Image, float]:
        raise NotImplementedError

    def close(self):
        pass


class StaticSource(MediaSource):
    def __init__(self, path: Path, target: tuple[int, int], fit: str, fps: float):
        with Image.open(path) as image:
            self.image = fit_image(image, target, fit)
        self.delay = 1.0 / fps

    def frame(self):
        return self.image.copy(), self.delay


class GifSource(MediaSource):
    def __init__(self, path: Path, target: tuple[int, int], fit: str, fps: float):
        self.path = path
        self.target = target
        self.fit = fit
        self.minimum_delay = 1.0 / fps
        self.image = Image.open(path)
        self.index = 0

    def frame(self):
        try:
            self.image.seek(self.index)
        except EOFError:
            self.index = 0
            self.image.seek(0)
        frame = fit_image(self.image.convert("RGB"), self.target, self.fit)
        delay = max(self.minimum_delay, float(self.image.info.get("duration", 100)) / 1000.0)
        self.index += 1
        return frame, delay

    def close(self):
        self.image.close()


class VideoSource(MediaSource):
    def __init__(self, path: Path, target: tuple[int, int], fit: str, fps: float):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for video loops; GIF and still images remain available")
        self.ffmpeg = ffmpeg
        self.path = path
        self.target = target
        self.fit = fit
        self.fps = fps
        self.process = None
        self._start()

    def _filter(self):
        width, height = self.target
        if self.fit == "stretch":
            return f"fps={self.fps},scale={width}:{height}"
        if self.fit == "contain":
            return (
                f"fps={self.fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x07090d"
            )
        return (
            f"fps={self.fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )

    def _start(self):
        self.process = subprocess.Popen(
            [
                self.ffmpeg,
                "-v", "error",
                "-stream_loop", "-1",
                "-i", str(self.path),
                "-an", "-sn", "-dn",
                "-vf", self._filter(),
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.process.stdout.read(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def frame(self):
        width, height = self.target
        expected = width * height * 3
        raw = self._read_exact(expected)
        if len(raw) != expected:
            self.close()
            self._start()
            raw = self._read_exact(expected)
        if len(raw) != expected:
            raise RuntimeError(f"ffmpeg produced an incomplete frame for {self.path}")
        return Image.frombytes("RGB", self.target, raw), 1.0 / self.fps

    def close(self):
        if self.process is None:
            return
        if self.process.stdout:
            self.process.stdout.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.process = None


def open_source(path: Path, layout: str, fit: str, fps: float) -> MediaSource:
    if not path.is_file():
        raise RuntimeError(f"media source does not exist: {path}")
    target = (TRIPTYCH_W, PANEL_H) if layout == "span" else (PANEL_W, PANEL_H)
    suffix = path.suffix.lower()
    if suffix == ".gif":
        return GifSource(path, target, fit, fps)
    if suffix in IMAGE_SUFFIXES:
        return StaticSource(path, target, fit, fps)
    if suffix in VIDEO_SUFFIXES:
        return VideoSource(path, target, fit, fps)
    raise RuntimeError(f"unsupported media type: {suffix or '(none)'}")


class PanelSourceSet(MediaSource):
    """Three independent decoders sampled once per synchronized USB update."""

    def __init__(self, paths: list[Path], fit: str, fps: float):
        if len(paths) != 3:
            raise RuntimeError("panel layout requires exactly three media sources")
        self.sources = []
        try:
            for path in paths:
                # mirror targets one native logical panel without duplicating it.
                self.sources.append(open_source(path, "mirror", fit, fps))
        except Exception:
            self.close()
            raise

    def frame(self):
        frames = []
        delays = []
        for source in self.sources:
            frame, delay = source.frame()
            frames.append(frame)
            delays.append(delay)
        return frames, min(delays)

    def close(self):
        for source in self.sources:
            source.close()
        self.sources = []


def split_frame(frame: Image.Image, layout: str, panel_order: list[int]):
    if layout == "mirror":
        return {index: frame.copy() for index in panel_order}
    return {
        index: frame.crop((column * PANEL_W, 0, (column + 1) * PANEL_W, PANEL_H))
        for column, index in enumerate(panel_order)
    }


def join_triptych(images: dict[int, Image.Image], panel_order: list[int]):
    frame = Image.new("RGB", (TRIPTYCH_W, PANEL_H), BG)
    for column, index in enumerate(panel_order):
        frame.paste(images[index], (column * PANEL_W, 0))
    return frame


def overlay_event(frame: Image.Image, event: dict | None):
    if not event or float(event.get("expires_at", 0)) <= time.time():
        return frame
    accent = {"pink": PINK, "yellow": YELLOW, "pale": PALE}.get(event.get("accent"), PINK)
    title = str(event.get("title", "")).upper()[:72]
    detail = str(event.get("detail", "")).upper()[:110]
    shade = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shade)
    draw.rectangle((0, 119, TRIPTYCH_W, PANEL_H), fill=(7, 9, 13, 230))
    draw.rectangle((18, 128, 24, 166), fill=accent + (255,))
    draw.line((24, 128, 57, 128), fill=accent + (255,), width=1)
    draw.text((48, 127), title, font=F_LABEL, fill=PALE + (255,))
    draw.text((48, 148), detail, font=F_MICRO, fill=MUTED + (255,))
    draw.text((1790, 142), "SHELL EVENT", font=F_MICRO, fill=accent + (255,))
    return Image.alpha_composite(frame.convert("RGBA"), shade).convert("RGB")


def thermal_event(metrics):
    values = []
    if metrics.cpu_temp is not None and metrics.cpu_temp >= 85:
        values.append(f"CPU {metrics.cpu_temp:.0f} C")
    if metrics.gpu_temp is not None and metrics.gpu_temp >= 90:
        values.append(f"GPU {metrics.gpu_temp:.0f} C")
    if not values:
        return None
    return {
        "title": "THERMAL LIMIT",
        "detail": " // ".join(values),
        "accent": "yellow",
        "expires_at": time.time() + 5,
    }


def merged_state(config: dict, runtime: dict):
    state = dict(config.get("display", {}))
    state.update({key: value for key, value in runtime.items() if key != "overlay"})
    if "overlay" in runtime:
        state["overlay"] = runtime["overlay"]
    return state


def source_key(state: dict):
    return (
        state.get("mode", "telemetry"),
        state.get("source", ""),
        tuple(state.get("sources", [])),
        state.get("layout", "span"),
        state.get("fit", "cover"),
        float(state.get("frames_per_second", 8.0)),
    )


def preview_source(config, path: Path, directory: Path, layout: str, fit: str, fps: float):
    order = [int(panel["index"]) for panel in config["panels"]]
    source = open_source(path, layout, fit, fps)
    try:
        frame, _ = source.frame()
    finally:
        source.close()
    images = split_frame(frame, layout, order)
    directory.mkdir(parents=True, exist_ok=True)
    frame = join_triptych(images, order)
    frame.save(directory / "media-triptych.png")
    for index, image in images.items():
        image.save(directory / f"media-panel-{index}.png")
    print(f"Media preview written to {directory}; no socket or USB access occurred.")


def preview_panels(config, paths: list[Path], directory: Path, fit: str, fps: float):
    order = [int(panel["index"]) for panel in config["panels"]]
    source = PanelSourceSet(paths, fit, fps)
    try:
        frames, _ = source.frame()
    finally:
        source.close()
    images = {index: frames[column] for column, index in enumerate(order)}
    directory.mkdir(parents=True, exist_ok=True)
    join_triptych(images, order).save(directory / "media-triptych.png")
    for index, image in images.items():
        image.save(directory / f"media-panel-{index}.png")
    print(f"Three-source preview written to {directory}; no socket or USB access occurred.")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--preview-source", type=Path)
    parser.add_argument("--preview-panels", nargs=3, type=Path, metavar=("LEFT", "CENTRE", "RIGHT"))
    parser.add_argument("--preview-dir", type=Path, default=Path("/tmp/lucille-zc360-media"))
    parser.add_argument("--layout", choices=("span", "mirror"), default="span")
    parser.add_argument("--fit", choices=("cover", "contain", "stretch"), default="cover")
    parser.add_argument("--fps", type=float, default=8.0)
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(args.config)
    if args.preview_source and args.preview_panels:
        raise SystemExit("choose either --preview-source or --preview-panels")
    if args.preview_source:
        preview_source(config, args.preview_source.expanduser().resolve(), args.preview_dir,
                       args.layout, args.fit, max(0.5, min(20.0, args.fps)))
        return
    if args.preview_panels:
        preview_panels(
            config,
            [path.expanduser().resolve() for path in args.preview_panels],
            args.preview_dir,
            args.fit,
            max(0.5, min(20.0, args.fps)),
        )
        return

    try:
        import psutil
        from jonsbo_fan_lib import (
            encode_fan_frames,
            send_fan_packet,
        )
    except ImportError as exc:
        raise SystemExit(f"live surface dependency missing: {exc}") from exc

    order = [int(panel["index"]) for panel in config["panels"]]
    collector = MetricCollector(str(config.get("gpu_device", "auto")))
    history = History()
    psutil.cpu_percent(interval=None)
    current_source = None
    current_key = None
    media_retry_after = 0.0
    last_metrics = 0.0
    metrics = collector.collect()
    history.push(metrics)
    logged_submit_error = None
    logged_media_error = None
    timeout = max(5.0, float(config.get("socket_timeout_seconds", 120.0)))
    sender = LatestPacketSender(
        send_fan_packet,
        timeout,
    )
    sender.start()
    print("Lucille ZC-360 unified surface - media + shell events + diagnostics")
    print(f"State: {state_path()}")

    try:
        while True:
            decode_seconds = 0.0
            compose_started = time.monotonic()
            runtime = read_state(state_path())
            state = merged_state(config, runtime)
            now = time.monotonic()
            if now - last_metrics >= 2.0:
                metrics = collector.collect()
                history.push(metrics)
                last_metrics = now

            event = thermal_event(metrics) or state.get("overlay")
            delay = max(0.5, float(config.get("refresh_seconds", 2.0)))

            layout = str(state.get("layout", "span"))
            media_requested = state.get("mode") == "media" and (
                state.get("source") or (layout == "panels" and len(state.get("sources", [])) == 3)
            )
            media_ready = False
            if media_requested:
                key = source_key(state)
                if key != current_key and now >= media_retry_after:
                    if current_source:
                        current_source.close()
                        current_source = None
                    try:
                        fps = max(0.5, min(20.0, float(state.get("frames_per_second", 8.0))))
                        fit = str(state.get("fit", "cover"))
                        if layout == "panels":
                            current_source = PanelSourceSet(
                                [Path(path) for path in state.get("sources", [])], fit, fps
                            )
                        else:
                            current_source = open_source(Path(state["source"]), layout, fit, fps)
                        current_key = key
                        logged_media_error = None
                        sender.reset_performance()
                        if layout == "panels":
                            print("Media: three independent left / centre / right loops")
                        else:
                            print(f"Media: {state['source']} ({layout})")
                    except Exception as exc:
                        current_key = None
                        media_retry_after = now + 5.0
                        message = str(exc)
                        if message != logged_media_error:
                            print(f"Media unavailable, showing diagnostics: {message}")
                            logged_media_error = message
                if current_source:
                    try:
                        decode_started = time.monotonic()
                        frame, delay = current_source.frame()
                        decode_seconds = time.monotonic() - decode_started
                        if layout == "panels":
                            images = {index: frame[column] for column, index in enumerate(order)}
                            if event:
                                combined = overlay_event(join_triptych(images, order), event)
                                images = split_frame(combined, "span", order)
                        else:
                            frame = overlay_event(frame, event)
                            images = split_frame(frame, layout, order)
                        media_ready = True
                    except Exception as exc:
                        current_source.close()
                        current_source = None
                        current_key = None
                        media_retry_after = now + 5.0
                        message = str(exc)
                        if message != logged_media_error:
                            print(f"Media decode failed, showing diagnostics: {message}")
                            logged_media_error = message

            if not media_ready:
                if not media_requested and current_source:
                    current_source.close()
                    current_source = None
                    current_key = None
                images = render_triptych(config, metrics, history)
                if event:
                    frame = overlay_event(join_triptych(images, order), event)
                    images = split_frame(frame, "span", order)

            compose_seconds = max(
                0.0,
                time.monotonic() - compose_started - decode_seconds,
            )
            started = time.monotonic()
            try:
                test_panels = os.environ.get("LUCILLE_ZC360_TEST_PANELS")
                test_panel = os.environ.get("LUCILLE_ZC360_TEST_PANEL")

                if test_panels:
                    indexes = [
                        int(value.strip())
                        for value in test_panels.split(",")
                        if value.strip()
                    ]
                    send_images = {
                        index: images[index]
                        for index in indexes
                    }
                elif test_panel is not None:
                    index = int(test_panel)
                    send_images = {index: images[index]}
                else:
                    send_images = images

                packet, encode_metrics = encode_fan_frames(send_images)

                sender.submit(
                    packet,
                    decode_seconds,
                    compose_seconds,
                    float(encode_metrics.get("encode_seconds", 0.0)),
                    int(encode_metrics.get("payload_bytes", len(packet))),
                    media_ready,
                )
                logged_submit_error = None

            except Exception as exc:
                message = str(exc)
                if message != logged_submit_error:
                    print(f"Surface frame submit failed: {message}")
                    logged_submit_error = message

            time.sleep(max(0.0, delay - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nSurface stopped; USB owner remains running.")
    finally:
        if current_source:
            current_source.close()
        sender.stop()


if __name__ == "__main__":
    main()

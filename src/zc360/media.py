"""Hardware-blind media decoding, fitting, and panel composition."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH
from .state import normalized_framing

PANEL_SIZE = (LOGICAL_WIDTH, LOGICAL_HEIGHT)
TRIPTYCH_SIZE = (LOGICAL_WIDTH * 3, LOGICAL_HEIGHT)
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def crop_box(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    framing: object = None,
) -> tuple[float, float, float, float]:
    """Return the exact source-space rectangle used by manual framing."""
    source_width, source_height = source_size
    target_width, target_height = target_size
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError("source and target dimensions must be positive")
    crop = normalized_framing(framing)
    source_aspect = source_width / source_height
    target_aspect = target_width / target_height
    if source_aspect >= target_aspect:
        base_height = float(source_height)
        base_width = base_height * target_aspect
    else:
        base_width = float(source_width)
        base_height = base_width / target_aspect
    width = base_width / crop["zoom"]
    height = base_height / crop["zoom"]
    left = (source_width - width) * crop["focus_x"]
    top = (source_height - height) * crop["focus_y"]
    return left, top, left + width, top + height


def fit_image(
    image: Image.Image,
    size: tuple[int, int],
    mode: str = "cover",
    framing: object = None,
) -> Image.Image:
    image = image.convert("RGB")
    if mode == "stretch":
        return image.resize(size, Image.Resampling.LANCZOS)
    if mode == "contain":
        contained = ImageOps.contain(image, size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, (7, 9, 13))
        canvas.paste(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
        return canvas

    active_framing = framing if mode == "manual" else None
    box = crop_box(image.size, size, active_framing)
    return image.crop(tuple(round(value) for value in box)).resize(size, Image.Resampling.LANCZOS)


def load_source_frame(path: Path) -> Image.Image:
    """Load one uncropped source frame for the visual framing editor."""
    if not path.is_file():
        raise RuntimeError(f"media source does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".gif" or suffix in IMAGE_SUFFIXES:
        with Image.open(path) as image:
            image.seek(0)
            return image.convert("RGB").copy()
    if suffix not in VIDEO_SUFFIXES:
        raise RuntimeError(f"unsupported media type: {suffix or '(none)'}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to preview video framing")
    try:
        result = subprocess.run(
            [
                ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not decode a framing preview for {path.name}") from exc
    try:
        with Image.open(io.BytesIO(result.stdout)) as image:
            return image.convert("RGB").copy()
    except Exception as exc:
        raise RuntimeError(f"ffmpeg returned an invalid preview for {path.name}") from exc


def preview_size(
    source_size: tuple[int, int],
    maximum: tuple[int, int] = (1280, 720),
) -> tuple[int, int]:
    """Bound an uncropped preview while preserving its source aspect ratio."""
    scale = min(1.0, maximum[0] / source_size[0], maximum[1] / source_size[1])
    return (
        max(1, round(source_size[0] * scale)),
        max(1, round(source_size[1] * scale)),
    )


def open_preview_source(path: Path, fps: float = 12.0) -> MediaSource:
    """Open an uncropped, lightweight source for the GUI framing editor."""
    first = load_source_frame(path)
    target = preview_size(first.size)
    first.close()
    suffix = path.suffix.lower()
    if suffix == ".gif":
        return GifSource(path, target, "stretch", fps)
    if suffix in VIDEO_SUFFIXES:
        return VideoSource(path, target, "stretch", fps)
    if suffix in IMAGE_SUFFIXES:
        return StaticSource(path, target, "stretch", fps)
    raise RuntimeError(f"unsupported media type: {suffix or '(none)'}")


class MediaSource:
    animated = True

    def frame(self) -> tuple[Image.Image, float]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StaticSource(MediaSource):
    animated = False

    def __init__(self, path: Path, target: tuple[int, int], fit: str, fps: float, framing=None):
        with Image.open(path) as image:
            self.image = fit_image(image, target, fit, framing)
        self.delay = 1.0 / fps

    def frame(self) -> tuple[Image.Image, float]:
        return self.image.copy(), self.delay


class GifSource(MediaSource):
    def __init__(self, path: Path, target: tuple[int, int], fit: str, fps: float, framing=None):
        self.target = target
        self.fit = fit
        self.framing = framing
        self.minimum_delay = 1.0 / fps
        self.image = Image.open(path)
        self.index = 0

    def frame(self) -> tuple[Image.Image, float]:
        try:
            self.image.seek(self.index)
        except EOFError:
            self.index = 0
            self.image.seek(0)
        duration = float(self.image.info.get("duration", 100)) / 1000.0
        frame = fit_image(self.image.convert("RGB"), self.target, self.fit, self.framing)
        self.index += 1
        return frame, max(self.minimum_delay, duration)

    def close(self) -> None:
        self.image.close()


class VideoSource(MediaSource):
    def __init__(self, path: Path, target: tuple[int, int], fit: str, fps: float, framing=None):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for video loops")
        self.ffmpeg = ffmpeg
        self.path = path
        self.target = target
        self.fit = fit
        self.fps = fps
        self.framing = normalized_framing(framing)
        self.process: subprocess.Popen | None = None
        self._start()

    def _filter(self) -> str:
        width, height = self.target
        if self.fit == "stretch":
            return f"fps={self.fps},scale={width}:{height}"
        if self.fit == "contain":
            return (
                f"fps={self.fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x07090d"
            )
        zoom = self.framing["zoom"] if self.fit == "manual" else 1.0
        focus_x = self.framing["focus_x"] if self.fit == "manual" else 0.5
        focus_y = self.framing["focus_y"] if self.fit == "manual" else 0.5
        scaled_w = max(width, round(width * zoom))
        scaled_h = max(height, round(height * zoom))
        return (
            f"fps={self.fps},scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-ow)*{focus_x:.6f}:(ih-oh)*{focus_y:.6f}"
        )

    def _start(self) -> None:
        self.process = subprocess.Popen(
            [
                self.ffmpeg, "-v", "error", "-stream_loop", "-1", "-i", str(self.path),
                "-an", "-sn", "-dn", "-vf", self._filter(), "-f", "rawvideo",
                "-pix_fmt", "rgb24", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _read_exact(self, size: int) -> bytes:
        if self.process is None or self.process.stdout is None:
            return b""
        data = bytearray()
        while len(data) < size:
            chunk = self.process.stdout.read(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def frame(self) -> tuple[Image.Image, float]:
        expected = self.target[0] * self.target[1] * 3
        raw = self._read_exact(expected)
        if len(raw) != expected:
            self.close()
            self._start()
            raw = self._read_exact(expected)
        if len(raw) != expected:
            raise RuntimeError(f"ffmpeg produced an incomplete frame for {self.path}")
        return Image.frombytes("RGB", self.target, raw), 1.0 / self.fps

    def close(self) -> None:
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


def open_source(
    path: Path,
    layout: str,
    fit: str,
    fps: float,
    framing=None,
) -> MediaSource:
    if not path.is_file():
        raise RuntimeError(f"media source does not exist: {path}")
    target = TRIPTYCH_SIZE if layout == "span" else PANEL_SIZE
    suffix = path.suffix.lower()
    if suffix == ".gif":
        return GifSource(path, target, fit, fps, framing)
    if suffix in IMAGE_SUFFIXES:
        return StaticSource(path, target, fit, fps, framing)
    if suffix in VIDEO_SUFFIXES:
        return VideoSource(path, target, fit, fps, framing)
    raise RuntimeError(f"unsupported media type: {suffix or '(none)'}")


class PanelSourceSet(MediaSource):
    def __init__(self, paths: list[Path], fit: str, fps: float, framings=None):
        if len(paths) != 3:
            raise RuntimeError("panel layout requires exactly three media sources")
        if not isinstance(framings, list) or len(framings) != 3:
            framings = [None, None, None]
        self.sources: list[MediaSource] = []
        try:
            for index, path in enumerate(paths):
                self.sources.append(open_source(path, "mirror", fit, fps, framings[index]))
        except Exception:
            self.close()
            raise
        self.animated = any(source.animated for source in self.sources)

    def frame(self) -> tuple[list[Image.Image], float]:
        frames: list[Image.Image] = []
        delays: list[float] = []
        for source in self.sources:
            frame, delay = source.frame()
            frames.append(frame)
            delays.append(delay)
        return frames, min(delays)

    def close(self) -> None:
        for source in self.sources:
            source.close()
        self.sources = []


def split_frame(frame: Image.Image, layout: str, panel_order: list[int]) -> dict[int, Image.Image]:
    if layout == "mirror":
        return {index: frame.copy() for index in panel_order}
    return {
        index: frame.crop((column * LOGICAL_WIDTH, 0, (column + 1) * LOGICAL_WIDTH, LOGICAL_HEIGHT))
        for column, index in enumerate(panel_order)
    }


def join_triptych(images: dict[int, Image.Image], panel_order: list[int]) -> Image.Image:
    frame = Image.new("RGB", TRIPTYCH_SIZE, (7, 9, 13))
    for column, index in enumerate(panel_order):
        frame.paste(images[index], (column * LOGICAL_WIDTH, 0))
    return frame


def render_first_frame(state: dict) -> dict[int, Image.Image]:
    """Render one hardware-blind preview using the same path as the renderer."""
    layout = state["layout"]
    order = state["panel_order"]
    fps = state["frames_per_second"]
    fit = state["fit"]
    if layout == "panels":
        source = PanelSourceSet(
            [Path(item) for item in state.get("sources", [])],
            fit,
            fps,
            state.get("panel_framing"),
        )
    else:
        source = open_source(Path(state.get("source", "")), layout, fit, fps, state.get("framing"))
    try:
        frame, _ = source.frame()
    finally:
        source.close()
    if layout == "panels":
        return {index: frame[column] for column, index in enumerate(order)}
    return split_frame(frame, layout, order)

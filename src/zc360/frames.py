"""Image conversion for the native portrait BGR888 framebuffer."""

from __future__ import annotations

from PIL import Image

from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH
from .protocol import build_frame_chunks, validate_frame_chunks


def normalize_image(image: Image.Image) -> Image.Image:
    if image.size != (LOGICAL_WIDTH, LOGICAL_HEIGHT):
        image = image.resize((LOGICAL_WIDTH, LOGICAL_HEIGHT))
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def native_bgr_bytes(image: Image.Image) -> bytes:
    logical = normalize_image(image)
    portrait = logical.rotate(-90, expand=True)
    red, green, blue = portrait.split()
    return Image.merge("RGB", (blue, green, red)).tobytes()


def prepare_frame_chunks(image: Image.Image) -> list[bytes]:
    chunks = build_frame_chunks(native_bgr_bytes(image))
    validate_frame_chunks(chunks)
    return chunks


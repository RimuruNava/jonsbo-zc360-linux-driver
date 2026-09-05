"""Versioned local IPC with safe fallback to the original frame protocol."""

from __future__ import annotations

import io
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from PIL import Image

from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH, PANEL_COUNT, socket_path

ESCAPE = 0x00
MAGIC = b"ZC36"
IPC_VERSION = 1

OP_HELLO = 1
OP_FRAME_SET = 2

STATUS_OK = 0
STATUS_BAD_REQUEST = 1
STATUS_PROCESSING_ERROR = 2
STATUS_UNSUPPORTED_VERSION = 3

REQUEST_HEADER = struct.Struct(">4sBBI")
RESPONSE_HEADER = struct.Struct(">4sBBI")
FRAME_ENTRY = struct.Struct(">BI")

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024


class IPCError(RuntimeError):
    """Raised when the daemon socket protocol is unavailable or invalid."""


class DaemonRejected(IPCError):
    """Raised when the daemon understood a request but could not perform it."""


@dataclass(frozen=True)
class Request:
    version: int
    opcode: int
    payload: bytes


@dataclass(frozen=True)
class Response:
    version: int
    status: int
    payload: dict[str, Any]


def recv_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError(f"socket closed after {len(data)}/{size} bytes")
        data.extend(chunk)
    return bytes(data)


def encode_request(opcode: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_REQUEST_BYTES:
        raise IPCError(f"request payload exceeds {MAX_REQUEST_BYTES} bytes")
    return bytes((ESCAPE,)) + REQUEST_HEADER.pack(MAGIC, IPC_VERSION, opcode, len(payload)) + payload


def receive_request(connection: socket.socket, first_byte: int) -> Request:
    if first_byte != ESCAPE:
        raise IPCError("versioned request does not begin with the escape byte")
    magic, version, opcode, length = REQUEST_HEADER.unpack(recv_exact(connection, REQUEST_HEADER.size))
    if magic != MAGIC:
        raise IPCError("invalid IPC magic")
    if length > MAX_REQUEST_BYTES:
        raise IPCError(f"request payload exceeds {MAX_REQUEST_BYTES} bytes")
    return Request(version=version, opcode=opcode, payload=recv_exact(connection, length))


def encode_response(status: int, payload: Mapping[str, Any] | None = None) -> bytes:
    body = json.dumps(dict(payload or {}), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return RESPONSE_HEADER.pack(MAGIC, IPC_VERSION, status, len(body)) + body


def receive_response(connection: socket.socket) -> Response:
    magic, version, status, length = RESPONSE_HEADER.unpack(recv_exact(connection, RESPONSE_HEADER.size))
    if magic != MAGIC:
        raise IPCError("invalid daemon response magic")
    if version != IPC_VERSION:
        raise IPCError(f"unsupported daemon IPC version {version}")
    if length > MAX_REQUEST_BYTES:
        raise IPCError("daemon response is unreasonably large")
    raw = recv_exact(connection, length)
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IPCError("daemon returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise IPCError("daemon response JSON must be an object")
    return Response(version=version, status=status, payload=payload)


def encode_fan_frames(images: Mapping[int, Image.Image]) -> tuple[bytes, dict[str, float | int]]:
    """Encode images into the original packet body used by both IPC versions.

    PPM is intentional: it avoids spending CPU on lossless compression for a
    local socket while preserving exact RGB pixels.
    """
    started = time.monotonic()
    if not 1 <= len(images) <= PANEL_COUNT:
        raise IPCError(f"a frame set must contain 1..{PANEL_COUNT} panels")

    packet = bytearray((len(images),))
    seen: set[int] = set()
    for index, image in sorted(images.items()):
        if index in seen or not 0 <= index < PANEL_COUNT:
            raise IPCError(f"panel index must be unique and in 0..{PANEL_COUNT - 1}")
        seen.add(index)
        rgb = image.convert("RGB")
        header = f"P6\n{rgb.width} {rgb.height}\n255\n".encode("ascii")
        data = header + rgb.tobytes()
        if len(data) > MAX_IMAGE_BYTES:
            raise IPCError(f"encoded image for panel {index} is too large")
        packet += FRAME_ENTRY.pack(index, len(data))
        packet += data

    encoded = bytes(packet)
    return encoded, {
        "encode_seconds": time.monotonic() - started,
        "payload_bytes": len(encoded),
    }


def _decode_image(data: bytes, index: int) -> Image.Image:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise IPCError(f"invalid image size for panel {index}: {len(data)}")
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            image = source.convert("RGB")
    except Exception as exc:
        raise IPCError(f"invalid image payload for panel {index}") from exc
    if image.width > 4096 or image.height > 4096:
        raise IPCError(f"image dimensions for panel {index} are too large")
    return image


def decode_frame_packet(packet: bytes) -> dict[int, Image.Image]:
    if not packet:
        raise IPCError("empty frame packet")
    count = packet[0]
    if not 1 <= count <= PANEL_COUNT:
        raise IPCError(f"panel count must be 1..{PANEL_COUNT}, got {count}")
    offset = 1
    frames: dict[int, Image.Image] = {}
    for _ in range(count):
        if len(packet) - offset < FRAME_ENTRY.size:
            raise IPCError("truncated frame entry")
        index, length = FRAME_ENTRY.unpack_from(packet, offset)
        offset += FRAME_ENTRY.size
        if index in frames or not 0 <= index < PANEL_COUNT:
            raise IPCError(f"invalid or duplicate panel index {index}")
        if length > MAX_IMAGE_BYTES or len(packet) - offset < length:
            raise IPCError(f"truncated or oversized image for panel {index}")
        frames[index] = _decode_image(packet[offset : offset + length], index)
        offset += length
    if offset != len(packet):
        raise IPCError("unexpected trailing frame-packet bytes")
    return frames


def receive_legacy_frames(connection: socket.socket, count: int) -> dict[int, Image.Image]:
    if not 1 <= count <= PANEL_COUNT:
        raise IPCError(f"legacy panel count must be 1..{PANEL_COUNT}, got {count}")
    frames: dict[int, Image.Image] = {}
    for _ in range(count):
        index, length = FRAME_ENTRY.unpack(recv_exact(connection, FRAME_ENTRY.size))
        if index in frames or not 0 <= index < PANEL_COUNT:
            raise IPCError(f"invalid or duplicate panel index {index}")
        if not 0 < length <= MAX_IMAGE_BYTES:
            raise IPCError(f"invalid image length for panel {index}: {length}")
        frames[index] = _decode_image(recv_exact(connection, length), index)
    return frames


_protocol_cache: dict[str, int] = {}
_cache_lock = threading.Lock()


def clear_protocol_cache() -> None:
    with _cache_lock:
        _protocol_cache.clear()


def _versioned_request(opcode: int, payload: bytes, timeout: float) -> Response:
    path = str(socket_path())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.sendall(encode_request(opcode, payload))
        return receive_response(connection)


def _negotiate(timeout: float) -> int:
    path = str(socket_path())
    with _cache_lock:
        cached = _protocol_cache.get(path)
    if cached is not None:
        return cached

    try:
        response = _versioned_request(OP_HELLO, b"", min(timeout, 2.0))
        version = IPC_VERSION if response.version == IPC_VERSION and response.status == STATUS_OK else 0
    except (EOFError, IPCError, OSError, socket.timeout):
        # The original daemon safely treats the escape byte as a zero-panel
        # packet and closes the connection without performing USB I/O.
        version = 0

    with _cache_lock:
        _protocol_cache[path] = version
    return version


def daemon_status(timeout: float = 2.0) -> dict[str, Any]:
    version = _negotiate(timeout)
    if version == 0:
        return {
            "driver": "legacy",
            "ipc_version": 0,
            "message": "legacy daemon is online; structured status is unavailable",
        }
    response = _versioned_request(OP_HELLO, b"", timeout)
    if response.status != STATUS_OK:
        raise DaemonRejected(response.payload.get("error", "daemon rejected status request"))
    return response.payload


def send_fan_packet(packet: bytes, timeout: float = 120.0) -> dict[str, Any]:
    """Send one already-encoded frame set and wait for a completed USB barrier."""
    started = time.monotonic()
    version = _negotiate(timeout)
    if version == IPC_VERSION:
        response = _versioned_request(OP_FRAME_SET, packet, timeout)
        if response.status != STATUS_OK:
            raise DaemonRejected(response.payload.get("error", "daemon rejected frame set"))
        return {
            "transfer_seconds": time.monotonic() - started,
            "ipc_version": IPC_VERSION,
            "daemon": response.payload,
        }

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path()))
        connection.sendall(packet)
        acknowledgement = recv_exact(connection, 1)
    if acknowledgement != b"\x01":
        raise DaemonRejected("legacy daemon reported a frame-processing error")
    return {
        "transfer_seconds": time.monotonic() - started,
        "ipc_version": 0,
    }


def send_fan_frames(images: Mapping[int, Image.Image], timeout: float = 120.0) -> dict[str, Any]:
    packet, metrics = encode_fan_frames(images)
    metrics.update(send_fan_packet(packet, timeout=timeout))
    return metrics

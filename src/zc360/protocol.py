"""Pure ZC-360 command and framebuffer protocol primitives.

This module performs no USB discovery or I/O and is safe to import in tests,
renderers, and configuration tools.
"""

from __future__ import annotations

from .constants import (
    CHANNEL_COMMAND,
    CHANNEL_FRAME,
    DES_KEY,
    FRAME_RECORD_COUNT,
    FRAME_RECORD_SIZE,
    FRAME_RECORDS_PER_WRITE,
    FRAME_WRITE_COUNT,
    FRAME_WRITE_SIZE,
    PIXEL_PAYLOAD_SIZE,
)


class ProtocolError(ValueError):
    """Raised when bytes do not match the validated ZC-360 protocol."""


def encrypt_command(command: int, parameter: int = 0) -> bytes:
    from Crypto.Cipher import DES

    if not 0 <= command <= 0xFF or not 0 <= parameter <= 0xFF:
        raise ProtocolError("command and parameter must be one byte each")
    plain = bytes((command, parameter, 0, 0, 0, 0, 0, 0))
    return DES.new(DES_KEY, DES.MODE_ECB).encrypt(plain)


def decrypt_payload(payload: bytes) -> bytes:
    from Crypto.Cipher import DES

    if len(payload) != 8:
        raise ProtocolError(f"encrypted command payload must be 8 bytes, got {len(payload)}")
    return DES.new(DES_KEY, DES.MODE_ECB).decrypt(payload)


def build_command_packet(command: int, parameter: int = 0) -> bytes:
    packet = bytearray(32)
    packet[0:2] = b"\xaa\x55"
    packet[2:10] = encrypt_command(command, parameter)
    packet[10] = CHANNEL_COMMAND
    packet[31] = 0xBB
    return bytes(packet)


def decrypt_response(response: bytes) -> bytes:
    if len(response) != 32:
        raise ProtocolError(f"controller response must be 32 bytes, got {len(response)}")
    decoded = bytearray(response)
    decoded[2:10] = decrypt_payload(bytes(decoded[2:10]))
    return bytes(decoded)


def frame_header(sequence: int) -> bytes:
    if not 1 <= sequence <= FRAME_RECORD_COUNT:
        raise ProtocolError(f"frame sequence must be 1..{FRAME_RECORD_COUNT}, got {sequence}")
    header = bytearray(32)
    header[0:2] = b"\xaa\x55"
    header[10] = CHANNEL_FRAME
    header[11] = (sequence >> 8) & 0xFF
    header[12] = sequence & 0xFF
    header[31] = 0xBB
    return bytes(header)


def build_frame_chunks(
    bgr_framebuffer: bytes,
    records_per_write: int = FRAME_RECORDS_PER_WRITE,
) -> list[bytes]:
    expected = FRAME_RECORD_COUNT * PIXEL_PAYLOAD_SIZE
    if len(bgr_framebuffer) != expected:
        raise ProtocolError(f"native framebuffer must be {expected} bytes, got {len(bgr_framebuffer)}")
    if records_per_write < 1 or FRAME_RECORD_COUNT % records_per_write:
        raise ProtocolError("records_per_write must divide the 720-record framebuffer")

    chunks: list[bytes] = []
    sequence = 1
    for first_record in range(0, FRAME_RECORD_COUNT, records_per_write):
        chunk = bytearray()
        for record in range(first_record, first_record + records_per_write):
            offset = record * PIXEL_PAYLOAD_SIZE
            chunk += frame_header(sequence)
            chunk += bgr_framebuffer[offset : offset + PIXEL_PAYLOAD_SIZE]
            sequence += 1
        chunks.append(bytes(chunk))

    if records_per_write == FRAME_RECORDS_PER_WRITE:
        if len(chunks) != FRAME_WRITE_COUNT or any(len(chunk) != FRAME_WRITE_SIZE for chunk in chunks):
            raise ProtocolError("validated transport requires exactly 90 writes of 4096 bytes")
    return chunks


def validate_frame_chunks(chunks: list[bytes]) -> None:
    if len(chunks) != FRAME_WRITE_COUNT:
        raise ProtocolError(f"expected {FRAME_WRITE_COUNT} frame writes, got {len(chunks)}")
    sequences: list[int] = []
    for chunk_index, chunk in enumerate(chunks):
        if len(chunk) != FRAME_WRITE_SIZE:
            raise ProtocolError(
                f"frame write {chunk_index} must be {FRAME_WRITE_SIZE} bytes, got {len(chunk)}"
            )
        for record_index in range(FRAME_RECORDS_PER_WRITE):
            offset = record_index * FRAME_RECORD_SIZE
            header = chunk[offset : offset + 32]
            if header[0:2] != b"\xaa\x55" or header[10] != CHANNEL_FRAME or header[31] != 0xBB:
                raise ProtocolError(
                    f"invalid frame header in write {chunk_index}, record {record_index}"
                )
            sequences.append((header[11] << 8) | header[12])
    if sequences != list(range(1, FRAME_RECORD_COUNT + 1)):
        raise ProtocolError("frame sequence is not exactly 1..720")

import importlib.util
import unittest

from PIL import Image

from zc360.constants import FRAME_RECORD_COUNT, FRAME_WRITE_COUNT, FRAME_WRITE_SIZE
from zc360.frames import native_bgr_bytes, prepare_frame_chunks
from zc360.protocol import (
    ProtocolError,
    build_command_packet,
    frame_header,
    validate_frame_chunks,
)


class ProtocolTests(unittest.TestCase):
    def test_solid_rgb_becomes_native_bgr(self):
        image = Image.new("RGB", (640, 180), (12, 34, 56))
        self.assertEqual(native_bgr_bytes(image)[:3], bytes((56, 34, 12)))

    def test_validated_frame_shape_and_sequence(self):
        chunks = prepare_frame_chunks(Image.new("RGB", (640, 180), "black"))
        self.assertEqual(len(chunks), FRAME_WRITE_COUNT)
        self.assertTrue(all(len(chunk) == FRAME_WRITE_SIZE for chunk in chunks))
        validate_frame_chunks(chunks)
        first = chunks[0][:32]
        last = chunks[-1][7 * 512 : 7 * 512 + 32]
        self.assertEqual((first[11] << 8) | first[12], 1)
        self.assertEqual((last[11] << 8) | last[12], FRAME_RECORD_COUNT)

    def test_frame_header_rejects_out_of_range_sequence(self):
        with self.assertRaises(ProtocolError):
            frame_header(0)
        with self.assertRaises(ProtocolError):
            frame_header(FRAME_RECORD_COUNT + 1)

    @unittest.skipUnless(importlib.util.find_spec("Crypto"), "pycryptodome is not installed")
    def test_command_packet_wire_shape(self):
        packet = build_command_packet(56, 0)
        self.assertEqual(len(packet), 32)
        self.assertEqual(packet[:2], b"\xaa\x55")
        self.assertEqual(packet[10], 1)
        self.assertEqual(packet[31], 0xBB)
        self.assertEqual(packet[2:10].hex(), "767623ac62ee3919")


if __name__ == "__main__":
    unittest.main()

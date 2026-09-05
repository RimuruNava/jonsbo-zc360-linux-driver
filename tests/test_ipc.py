import socket
import unittest
from unittest.mock import patch

from PIL import Image

from zc360.ipc import (
    IPC_VERSION,
    OP_FRAME_SET,
    STATUS_OK,
    IPCError,
    decode_frame_packet,
    encode_fan_frames,
    encode_request,
    encode_response,
    clear_protocol_cache,
    _negotiate,
    receive_request,
    receive_response,
)


class IPCTests(unittest.TestCase):
    def test_frame_packet_round_trip_is_deterministic(self):
        images = {
            2: Image.new("RGB", (640, 180), (3, 4, 5)),
            0: Image.new("RGB", (640, 180), (1, 2, 3)),
        }
        packet, metrics = encode_fan_frames(images)
        self.assertEqual(packet[0], 2)
        self.assertEqual(packet[1], 0)
        decoded = decode_frame_packet(packet)
        self.assertEqual(sorted(decoded), [0, 2])
        self.assertEqual(decoded[0].getpixel((0, 0)), (1, 2, 3))
        self.assertEqual(metrics["payload_bytes"], len(packet))

    def test_duplicate_panel_is_rejected(self):
        image = Image.new("RGB", (640, 180), "black")
        packet, _ = encode_fan_frames({0: image})
        duplicate = bytes((2,)) + packet[1:] + packet[1:]
        with self.assertRaises(IPCError):
            decode_frame_packet(duplicate)

    def test_versioned_request_and_response_round_trip(self):
        client, server = socket.socketpair()
        try:
            request_bytes = encode_request(OP_FRAME_SET, b"payload")
            client.sendall(request_bytes)
            first = server.recv(1)[0]
            request = receive_request(server, first)
            self.assertEqual(request.version, IPC_VERSION)
            self.assertEqual(request.opcode, OP_FRAME_SET)
            self.assertEqual(request.payload, b"payload")

            server.sendall(encode_response(STATUS_OK, {"mode": "test"}))
            response = receive_response(client)
            self.assertEqual(response.status, STATUS_OK)
            self.assertEqual(response.payload, {"mode": "test"})
        finally:
            client.close()
            server.close()

    def test_negotiation_safely_falls_back_when_old_daemon_closes_probe(self):
        clear_protocol_cache()
        with patch("zc360.ipc._versioned_request", side_effect=EOFError):
            self.assertEqual(_negotiate(2.0), 0)
        clear_protocol_cache()


if __name__ == "__main__":
    unittest.main()

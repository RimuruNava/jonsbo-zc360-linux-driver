import socket
import unittest
import io
from contextlib import redirect_stdout

from zc360.daemon import DriverState, serve_connection
from zc360.ipc import IPC_VERSION, OP_HELLO, STATUS_OK, encode_request, receive_response


class DaemonControlTests(unittest.TestCase):
    def test_hello_does_not_touch_transport(self):
        client, server = socket.socketpair()
        state = DriverState(labels=["a", "b", "c"], warmed=[True, True, True])
        try:
            client.sendall(encode_request(OP_HELLO))
            serve_connection(server, None, None, [], state)
            response = receive_response(client)
            self.assertEqual(response.status, STATUS_OK)
            self.assertEqual(response.payload["ipc_version"], IPC_VERSION)
            self.assertEqual([panel["serial"] for panel in response.payload["panels"]], ["a", "b", "c"])
            self.assertEqual(state.frame_sets, 0)
        finally:
            client.close()
            server.close()

    def test_performance_logging_is_rate_limited(self):
        state = DriverState(labels=["a", "b", "c"], warmed=[True, True, True])
        transfer = {
            "mode": "async-triplet",
            "panels": [0, 1, 2],
            "packet_ms": 62.0,
            "usb_ms": 60.0,
            "statuses": [0x62, 0x62, 0x62],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            state.record_transfer(transfer)
            state.record_transfer(transfer)
            state.record_transfer(transfer)
        self.assertEqual(output.getvalue().count("USB PERF"), 1)
        self.assertIn("USB PERF first", output.getvalue())


if __name__ == "__main__":
    unittest.main()

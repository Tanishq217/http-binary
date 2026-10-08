"""Integration tests for bserve server (SPEC Section 1, 3, 5)."""

import os
import unittest

from hbin import wire
from tests.support import ServerCase


class ServerTests(ServerCase):
    def test_get_file_success(self):
        conn = self.connect()
        conn.request(1, "/index.html")
        status, headers, body, frames = conn.exchange()

        self.assertEqual(status, 200)
        self.assertIn("<h1>Hello from HBIN/1!</h1>", body.decode("utf-8"))
        self.assertEqual(headers["server"], "bserve/1")
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["content-length"], str(len(body)))
        self.assertIn("last-modified", headers)
        self.assertIn("etag", headers)

        # 2 frames received: RESPONSE (without END_STREAM) and DATA (with END_STREAM)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].type, wire.T_RESPONSE)
        self.assertFalse(frames[0].end_stream)
        self.assertEqual(frames[1].type, wire.T_DATA)
        self.assertTrue(frames[1].end_stream)

    def test_get_root_defaults_to_index_html(self):
        conn = self.connect()
        conn.request(1, "/")
        status, _, body, _ = conn.exchange()
        self.assertEqual(status, 200)
        self.assertIn("<h1>Hello from HBIN/1!</h1>", body.decode("utf-8"))

    def test_get_nested_file(self):
        conn = self.connect()
        conn.request(1, "/sub/note.txt")
        status, _, body, _ = conn.exchange()
        self.assertEqual(status, 200)
        self.assertIn("nested inside the sub/ subdirectory", body.decode("utf-8"))

    def test_head_request_no_data_frames(self):
        conn = self.connect()
        conn.request(1, "/index.html", method=wire.M_HEAD)
        status, headers, body, frames = conn.exchange()

        self.assertEqual(status, 200)
        self.assertEqual(body, b"")  # No body transmitted
        self.assertEqual(len(frames), 1)  # Only RESPONSE frame
        self.assertEqual(frames[0].type, wire.T_RESPONSE)
        self.assertTrue(frames[0].end_stream)  # END_STREAM on RESPONSE frame
        self.assertIn("content-length", headers)
        self.assertGreater(int(headers["content-length"]), 0)

    def test_empty_file_response(self):
        conn = self.connect()
        conn.request(1, "/empty.txt")
        status, headers, body, frames = conn.exchange()

        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["content-length"], "0")
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].end_stream)

    def test_not_found_404(self):
        conn = self.connect()
        conn.request(1, "/nonexistent-page.html")
        status, headers, body, _ = conn.exchange()

        self.assertEqual(status, 404)
        self.assertIn(b"404 Resource '/nonexistent-page.html' not found", body)
        self.assertEqual(headers["server"], "bserve/1")
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")

    def test_path_traversal_prevention(self):
        conn = self.connect()
        conn.request(1, "/../bserve")
        status, _, _, _ = conn.exchange()
        self.assertEqual(status, 404)

        conn.request(2, "/sub/../../bserve")
        status2, _, _, _ = conn.exchange()
        self.assertEqual(status2, 404)

    def test_method_not_allowed_405(self):
        conn = self.connect()
        conn.request(1, "/index.html", method=7)  # Method 7 unsupported
        status, headers, body, _ = conn.exchange()

        self.assertEqual(status, 405)
        self.assertEqual(headers.get("allow"), "GET, HEAD")
        self.assertIn(b"Method METHOD_7 not supported", body)

    def test_if_none_match_caching_304(self):
        conn = self.connect()

        # Initial request to get ETag
        conn.request(1, "/index.html")
        status, headers, _, _ = conn.exchange()
        self.assertEqual(status, 200)
        etag = headers["etag"]

        # Conditional request with matching etag
        conn.request(2, "/index.html", headers=[("if-none-match", etag)])
        status2, _, body2, frames2 = conn.exchange()

        self.assertEqual(status2, 304)
        self.assertEqual(body2, b"")
        self.assertEqual(len(frames2), 1)
        self.assertTrue(frames2[0].end_stream)

    def test_keep_alive_multiple_sequential_requests(self):
        conn = self.connect()

        # Request 1
        conn.request(1, "/hello.txt")
        status1, _, body1, _ = conn.exchange()
        self.assertEqual(status1, 200)
        self.assertIn(b"Hello from the HBIN/1 binary protocol", body1)

        # Request 2 on same socket
        conn.request(2, "/style.css")
        status2, headers2, body2, _ = conn.exchange()
        self.assertEqual(status2, 200)
        self.assertIn(b"font-family", body2)
        self.assertEqual(headers2["content-type"], "text/css; charset=utf-8")

        # Request 3 on same socket
        conn.request(3, "/empty.txt")
        status3, _, body3, _ = conn.exchange()
        self.assertEqual(status3, 200)
        self.assertEqual(body3, b"")

    def test_malformed_request_returns_400_and_keeps_connection_open(self):
        conn = self.connect()

        # Malformed request: path does not start with '/'
        bad_payload = b"\x01\x00\x08bad_path"
        conn.send_frame(wire.T_REQUEST, wire.F_END_STREAM, 1, bad_payload)
        status, _, body, _ = conn.exchange()

        self.assertEqual(status, 400)
        self.assertIn(b"request path must start with '/'", body)

        # SPEC REQUIREMENT: Connection must be kept open! Subsequent valid request succeeds
        conn.request(2, "/index.html")
        status2, _, body2, _ = conn.exchange()
        self.assertEqual(status2, 200)
        self.assertIn("<h1>Hello from HBIN/1!</h1>", body2.decode("utf-8"))

    def test_frame_too_large_returns_400_and_closes_connection(self):
        conn = self.connect()

        # Send header announcing payload of 20,000 bytes (> 16384 limit)
        bad_header = (20000).to_bytes(3, "big") + bytes([wire.T_REQUEST, wire.F_END_STREAM]) + (1).to_bytes(3, "big")
        conn.send_raw(bad_header)
        status, _, body, _ = conn.exchange()

        self.assertEqual(status, 400)
        self.assertIn(b"exceeds maximum 16384 bytes", body)

        # Subsequent read should hit EOF because connection is closed
        next_frame = conn.read_frame()
        self.assertIsNone(next_frame)

    def test_unknown_frame_type_skipped_cleanly(self):
        conn = self.connect()

        # Send unknown frame type 0x77 before valid request
        conn.send_frame(0x77, 0, 0, b"future-feature-data")

        # Follow with standard request
        conn.request(1, "/hello.txt")
        status, _, body, _ = conn.exchange()

        self.assertEqual(status, 200)
        self.assertIn(b"Hello from the HBIN/1 binary protocol", body)


if __name__ == "__main__":
    unittest.main()

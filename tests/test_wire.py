"""Unit tests for binary wire codec (SPEC Section 2, 3, 4)."""

import io
import unittest

from hbin import wire


class WireFrameTests(unittest.TestCase):
    def test_header_layout(self):
        # Frame: Type=DATA (0x03), Flags=END_STREAM (0x01), StreamID=0x010203, Payload="abcd" (4 bytes)
        raw = wire.encode_frame(wire.T_DATA, wire.F_END_STREAM, 0x010203, b"abcd")
        expected_header = bytes.fromhex("000004 03 01 010203")
        self.assertEqual(raw, expected_header + b"abcd")
        self.assertEqual(len(raw), 8 + 4)

    def test_roundtrip_frame(self):
        original = wire.Frame(wire.T_RESPONSE, 0, 42, b"status-data")
        encoded = original.encode()
        stream = io.BytesIO(encoded)
        decoded = wire.read_frame(stream)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded, original)
        self.assertEqual(decoded.stream_id, 42)
        self.assertEqual(decoded.type, wire.T_RESPONSE)
        self.assertEqual(decoded.payload, b"status-data")

    def test_clean_eof_at_boundary(self):
        stream = io.BytesIO(b"")
        self.assertIsNone(wire.read_frame(stream))

    def test_truncated_header(self):
        stream = io.BytesIO(b"\x00\x00\x05\x01\x00")  # Only 5 bytes instead of 8
        with self.assertRaises(wire.Truncated):
            wire.read_frame(stream)

    def test_truncated_payload(self):
        header = wire.encode_frame(wire.T_DATA, 0, 1, b"12345")[:8]
        stream = io.BytesIO(header + b"12")  # 2 bytes given, 5 expected
        with self.assertRaises(wire.Truncated):
            wire.read_frame(stream)

    def test_frame_too_large(self):
        # Construct header claiming 20,000 bytes payload (> 16384)
        bad_header = (20000).to_bytes(3, "big") + bytes([wire.T_DATA, 0]) + (1).to_bytes(3, "big")
        stream = io.BytesIO(bad_header)
        with self.assertRaises(wire.FrameTooLarge) as ctx:
            wire.read_frame(stream)
        self.assertEqual(ctx.exception.length, 20000)
        self.assertEqual(ctx.exception.stream_id, 1)

    def test_stream_id_bounds(self):
        with self.assertRaises(ValueError):
            wire.encode_frame(wire.T_DATA, 0, -1, b"")
        with self.assertRaises(ValueError):
            wire.encode_frame(wire.T_DATA, 0, 0x1000000, b"")  # > 24 bits


class WireHeaderTests(unittest.TestCase):
    def test_indexed_headers(self):
        # host (ID 1) and user-agent (ID 2)
        headers = [("host", "localhost:9000"), ("user-agent", "bcurl/1")]
        encoded = wire.encode_headers(headers)

        # ID 1 (1 byte) + len 14 (2 bytes) + "localhost:9000" + ID 2 + len 7 + "bcurl/1"
        expected = (
            b"\x01\x00\x0elocalhost:9000"
            b"\x02\x00\x07bcurl/1"
        )
        self.assertEqual(encoded, expected)

        decoded = wire.decode_headers(encoded)
        self.assertEqual(decoded, headers)

    def test_literal_custom_header(self):
        headers = [("x-custom-metric", "42")]
        encoded = wire.encode_headers(headers)

        # 0x00 + name_len (15) + "x-custom-metric" + value_len (2) + "42"
        expected = b"\x00\x0fx-custom-metric\x00\x0242"
        self.assertEqual(encoded, expected)

        decoded = wire.decode_headers(encoded)
        self.assertEqual(decoded, headers)

    def test_unknown_indexed_id_skipped(self):
        # ID 45 is unknown (reserved 11..255). Protocol requires skipping value.
        raw = (
            b"\x01\x00\x03foo"            # host: foo
            b"\x2d\x00\x04skip"           # ID 45 (0x2d), length 4, "skip"
            b"\x03\x00\x03*/*"            # accept: */*
        )
        decoded = wire.decode_headers(raw)
        self.assertEqual(decoded, [("host", "foo"), ("accept", "*/*")])

    def test_malformed_header_past_boundary(self):
        # Header entry claims 10-byte value but only 3 bytes remain
        raw = b"\x01\x00\x0aabc"
        with self.assertRaises(wire.Malformed):
            wire.decode_headers(raw)


class WirePayloadTests(unittest.TestCase):
    def test_request_roundtrip(self):
        payload = wire.encode_request(wire.M_GET, "/docs/spec.txt", [("host", "example.com")])
        method, path, headers = wire.decode_request(payload)
        self.assertEqual(method, wire.M_GET)
        self.assertEqual(path, "/docs/spec.txt")
        self.assertEqual(headers, [("host", "example.com")])

    def test_request_path_validation(self):
        # Path without leading slash
        with self.assertRaises(wire.Malformed):
            wire.decode_request(b"\x01\x00\x04file")

        # Path with NUL byte
        with self.assertRaises(wire.Malformed):
            wire.decode_request(b"\x01\x00\x06/a\x00b")

        # Request payload too short
        with self.assertRaises(wire.Malformed):
            wire.decode_request(b"\x01\x00")

    def test_response_roundtrip(self):
        payload = wire.encode_response(200, [("server", "bserve/1"), ("content-length", "100")])
        status, headers = wire.decode_response(payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers, [("server", "bserve/1"), ("content-length", "100")])

    def test_response_too_short(self):
        with self.assertRaises(wire.Malformed):
            wire.decode_response(b"\x00")


if __name__ == "__main__":
    unittest.main()

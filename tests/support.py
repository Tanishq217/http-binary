"""Test support and fixture utilities for HBIN/1 testing."""

import os
import socket
import threading
import unittest
from typing import BinaryIO

from hbin import wire
from hbin.server import BinaryHTTPHandler, ThreadedServer


class TestClientConn:
    """Convenience wrapper around a client socket for test assertions."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.rfile: BinaryIO = sock.makefile("rb")
        self.wfile: BinaryIO = sock.makefile("wb")

    def close(self):
        for stream in (self.wfile, self.rfile):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send_raw(self, raw_bytes: bytes):
        self.wfile.write(raw_bytes)
        self.wfile.flush()

    def send_frame(self, type_: int, flags: int, stream_id: int, payload: bytes = b""):
        data = wire.encode_frame(type_, flags, stream_id, payload)
        self.send_raw(data)

    def request(self, stream_id: int, path: str, method: int = wire.M_GET, headers: list[tuple[str, str]] | None = None):
        hdrs = [("host", "localhost"), ("user-agent", "test/1"), ("accept", "*/*")]
        if headers:
            hdrs.extend(headers)
        payload = wire.encode_request(method, path, hdrs)
        self.send_frame(wire.T_REQUEST, wire.F_END_STREAM, stream_id, payload)

    def read_frame(self) -> wire.Frame | None:
        return wire.read_frame(self.rfile)

    def exchange(self) -> tuple[int, dict[str, str], bytes, list[wire.Frame]]:
        """Read frames until END_STREAM, returning (status, headers_dict, body, frames)."""
        frames = []
        status: int | None = None
        headers: dict[str, str] = {}
        body = bytearray()

        while True:
            frame = self.read_frame()
            if frame is None:
                break
            frames.append(frame)

            if frame.type == wire.T_RESPONSE:
                status, raw_headers = wire.decode_response(frame.payload)
                headers = {k.lower(): v for k, v in raw_headers}
                if frame.end_stream:
                    break
            elif frame.type == wire.T_DATA:
                body.extend(frame.payload)
                if frame.end_stream:
                    break

        assert status is not None, "Exchange completed without receiving RESPONSE frame"
        return status, headers, bytes(body), frames


class ServerCase(unittest.TestCase):
    """Base test case that spins up a real bserve server on an ephemeral port."""

    def setUp(self):
        self.root_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "www")
        self.server = ThreadedServer(("127.0.0.1", 0), BinaryHTTPHandler, self.root_dir)
        self.host, self.port = self.server.server_address[:2]

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self._connections: list[TestClientConn] = []

    def tearDown(self):
        for conn in self._connections:
            conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def connect(self, timeout: float = 5.0) -> TestClientConn:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(timeout)
        conn = TestClientConn(sock)
        self._connections.append(conn)
        return conn

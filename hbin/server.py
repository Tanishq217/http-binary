"""bserve: HBIN/1 binary protocol file server.

Implements Track 1 server requirements per SPEC.md:
- Accepts TCP connections and keeps them open (keep-alive)
- Reads binary request frames
- Resolves paths securely under root directory
- Sends RESPONSE and chunked DATA frames
- Handles 200, 304 (ETag caching), 400 (malformed frames), 404, 405 (method not allowed), 500
- Cleanly skips unknown frame types to guarantee future protocol extensibility
"""

import email.utils
import mimetypes
import os
import socket
import socketserver
import sys
from typing import BinaryIO

from . import wire

SERVER_NAME = "bserve/1"
DEFAULT_PORT = 9000
IDLE_TIMEOUT = 120.0  # seconds before closing idle connection


def guess_content_type(file_path: str) -> str:
    """Guess MIME type and append UTF-8 charset for text types."""
    mime, _ = mimetypes.guess_type(file_path)
    if mime is None:
        return "application/octet-stream"
    if mime.startswith("text/") or mime in ("application/json", "application/javascript", "application/xml"):
        return f"{mime}; charset=utf-8"
    return mime


def format_etag(stat_result: os.stat_result) -> str:
    """Generate HTTP entity tag from file size and modification nanoseconds."""
    return f'"{stat_result.st_size:x}-{stat_result.st_mtime_ns:x}"'


def resolve_path(root_dir: str, requested_path: str) -> str | None:
    """Resolve request path to regular file within root directory.

    Returns canonical path if valid regular file, or None if not found / path traversal.
    """
    rel = requested_path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"

    canonical_root = os.path.realpath(root_dir)
    target_path = os.path.realpath(os.path.join(canonical_root, rel))

    # Reject path traversal (e.g., ../ or symlinks pointing outside root)
    if os.path.commonpath([canonical_root, target_path]) != canonical_root:
        return None

    if os.path.isfile(target_path):
        return target_path
    return None


class BinaryHTTPHandler(socketserver.BaseRequestHandler):
    """Handler for persistent HBIN/1 TCP connections."""

    def setup(self):
        self.request.settimeout(IDLE_TIMEOUT)
        self.rfile: BinaryIO = self.request.makefile("rb")
        self.wfile: BinaryIO = self.request.makefile("wb")
        self.client_ip, self.client_port = self.client_address[:2]

    def finish(self):
        for stream in (self.wfile, self.rfile):
            try:
                stream.close()
            except OSError:
                pass

    def log(self, message: str):
        print(f"[{self.client_ip}:{self.client_port}] {message}", file=sys.stderr, flush=True)

    def send_error(self, stream_id: int, status: int, message: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        """Send error response with text/plain body."""
        body = f"{status} {message}\n".encode("utf-8")
        headers = [
            ("server", SERVER_NAME),
            ("content-type", "text/plain; charset=utf-8"),
            ("content-length", str(len(body))),
        ]
        if extra_headers:
            headers.extend(extra_headers)

        response_payload = wire.encode_response(status, headers)
        self.wfile.write(wire.encode_frame(wire.T_RESPONSE, 0, stream_id, response_payload))
        self.wfile.write(wire.encode_frame(wire.T_DATA, wire.F_END_STREAM, stream_id, body))
        self.wfile.flush()

    def handle(self):
        """Keep-alive connection loop processing binary frames."""
        root_dir = self.server.root_dir

        while True:
            try:
                frame = wire.read_frame(self.rfile)
            except socket.timeout:
                self.log("Connection timed out waiting for request")
                break
            except wire.FrameTooLarge as err:
                self.log(f"Malformed: {err} - closing connection")
                try:
                    self.send_error(err.stream_id, 400, "Frame payload exceeds maximum 16384 bytes")
                except OSError:
                    pass
                break
            except (wire.Truncated, ConnectionResetError, BrokenPipeError) as err:
                self.log(f"Connection ended abruptly: {err}")
                break
            except OSError as err:
                self.log(f"Socket I/O error: {err}")
                break

            # Clean EOF on frame boundary
            if frame is None:
                self.log("Client closed connection cleanly")
                break

            # SPEC Section 3: Skip unknown frame types cleanly
            if frame.type not in wire.KNOWN_TYPES:
                self.log(f"Skipping unknown frame type 0x{frame.type:02x} (len={len(frame.payload)})")
                continue

            # Clients may only send REQUEST frames (0x01)
            if frame.type != wire.T_REQUEST:
                self.log(f"Malformed: client sent invalid frame type 0x{frame.type:02x}")
                self.send_error(frame.stream_id, 400, f"Frame type 0x{frame.type:02x} is not allowed from client")
                continue

            # Validate REQUEST frame flags and stream ID
            if not frame.end_stream:
                self.log("Malformed: REQUEST frame missing END_STREAM flag")
                self.send_error(frame.stream_id, 400, "REQUEST frame must have END_STREAM flag set")
                continue

            if frame.stream_id == 0:
                self.log("Malformed: REQUEST frame with reserved stream ID 0")
                self.send_error(0, 400, "Stream ID 0 is reserved for connection-level frames")
                continue

            # Decode request payload
            try:
                method, path, headers = wire.decode_request(frame.payload)
            except wire.Malformed as err:
                self.log(f"Malformed request payload: {err}")
                self.send_error(frame.stream_id, 400, f"Malformed request: {err}")
                continue

            method_name = wire.METHOD_NAMES.get(method, f"METHOD_{method}")
            self.log(f"Stream {frame.stream_id}: {method_name} {path}")

            # Check method support (1=GET, 2=HEAD)
            if method not in (wire.M_GET, wire.M_HEAD):
                self.log(f"Method not allowed: {method}")
                self.send_error(frame.stream_id, 405, f"Method {method_name} not supported", [("allow", "GET, HEAD")])
                continue

            # Resolve requested file under root
            file_path = resolve_path(root_dir, path)
            if file_path is None:
                self.log(f"File not found: {path}")
                self.send_error(frame.stream_id, 404, f"Resource '{path}' not found")
                continue

            # Stat file to get metadata
            try:
                st = os.stat(file_path)
            except OSError as err:
                self.log(f"Filesystem error stating {file_path}: {err}")
                self.send_error(frame.stream_id, 500, "Filesystem error accessing resource")
                continue

            etag_val = format_etag(st)
            last_mod_val = email.utils.formatdate(st.st_mtime, usegmt=True)
            content_type_val = guess_content_type(file_path)
            content_len_val = str(st.st_size)

            # Check conditional request: If-None-Match (Section 5)
            header_dict = {k.lower(): v for k, v in headers}
            client_etag = header_dict.get("if-none-match")
            if client_etag and client_etag.strip() == etag_val:
                self.log(f"Stream {frame.stream_id}: 304 Not Modified")
                res_headers = [
                    ("server", SERVER_NAME),
                    ("etag", etag_val),
                    ("last-modified", last_mod_val),
                ]
                # 304 response carries no body: END_STREAM is set on RESPONSE frame
                res_payload = wire.encode_response(304, res_headers)
                self.wfile.write(wire.encode_frame(wire.T_RESPONSE, wire.F_END_STREAM, frame.stream_id, res_payload))
                self.wfile.flush()
                continue

            # Build successful 200 headers
            response_headers = [
                ("server", SERVER_NAME),
                ("content-type", content_type_val),
                ("content-length", content_len_val),
                ("last-modified", last_mod_val),
                ("etag", etag_val),
            ]

            is_head = (method == wire.M_HEAD)
            is_empty_file = (st.st_size == 0)

            if is_head or is_empty_file:
                # No body to transmit: set END_STREAM on RESPONSE frame
                res_payload = wire.encode_response(200, response_headers)
                self.wfile.write(wire.encode_frame(wire.T_RESPONSE, wire.F_END_STREAM, frame.stream_id, res_payload))
                self.wfile.flush()
                self.log(f"Stream {frame.stream_id}: 200 OK (no body, size={st.st_size})")
                continue

            # Send 200 RESPONSE frame without END_STREAM (DATA frames will follow)
            res_payload = wire.encode_response(200, response_headers)
            self.wfile.write(wire.encode_frame(wire.T_RESPONSE, 0, frame.stream_id, res_payload))

            # Stream body in chunks of at most MAX_PAYLOAD bytes
            bytes_sent = 0
            try:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(wire.MAX_PAYLOAD)
                        if not chunk:
                            break
                        bytes_sent += len(chunk)
                        is_last = (bytes_sent >= st.st_size)
                        flags = wire.F_END_STREAM if is_last else 0
                        self.wfile.write(wire.encode_frame(wire.T_DATA, flags, frame.stream_id, chunk))
                self.wfile.flush()
                self.log(f"Stream {frame.stream_id}: 200 OK sent {bytes_sent} bytes")
            except OSError as err:
                self.log(f"Error reading file {file_path}: {err}")
                break


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded TCP server with address reuse."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, root_dir):
        self.root_dir = os.path.realpath(root_dir)
        super().__init__(server_address, RequestHandlerClass)


def run_server(root_dir: str, port: int = DEFAULT_PORT, host: str = "0.0.0.0"):
    """Start bserve daemon serving files from root_dir on host:port."""
    if not os.path.isdir(root_dir):
        print(f"Error: root directory '{root_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    server = ThreadedServer((host, port), BinaryHTTPHandler, root_dir)
    print(f"bserve running on {host}:{port} serving '{os.path.realpath(root_dir)}'...", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.", file=sys.stderr)
    finally:
        server.server_close()


def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: ./bserve <root_dir> [port]", file=sys.stderr)
        print("Example: ./bserve ./www 9000", file=sys.stderr)
        sys.exit(1)

    root = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) == 3 else DEFAULT_PORT
    run_server(root, port)


if __name__ == "__main__":
    main()

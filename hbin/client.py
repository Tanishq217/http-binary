"""bcurl: HBIN/1 binary protocol client.

Implements Track 2 client requirements per SPEC.md:
- Builds binary REQUEST frames with END_STREAM
- Sends standard headers: host, user-agent, accept
- Reads RESPONSE and DATA frames
- Dumps body to stdout
- -v flag prints annotated hexdump for every transmitted and received frame
- Exits non-zero (exit code 22) on 4xx / 5xx HTTP status codes
- Reuses the same TCP socket across multiple path requests ("never open a second connection")
- Cleanly skips unknown frame types encountered on the wire
"""

import socket
import sys
from typing import BinaryIO

from . import wire
from .hexdump import hexdump

USER_AGENT = "bcurl/1"
DEFAULT_PORT = 9000
DEFAULT_TIMEOUT = 15.0

# Exit status codes
EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 1
EXIT_NETWORK_ERROR = 2
EXIT_PROTOCOL_ERROR = 3
EXIT_HTTP_ERROR = 22  # Matches curl --fail convention


class ProtocolError(Exception):
    """Raised when server violates wire protocol rules."""


def parse_target(target_str: str) -> tuple[str, int, str]:
    """Parse '[http://]host[:port][/path]' into (host, port, path).

    Defaults: port 9000, path '/'.
    """
    raw = target_str
    if "://" in raw:
        raw = raw.split("://", 1)[1]

    authority, slash, path = raw.partition("/")
    clean_path = "/" + path if slash else "/"

    # Handle IPv6 bracket format: [::1]:9000
    if authority.startswith("["):
        host_part, _, tail = authority[1:].partition("]")
        port = int(tail[1:]) if tail.startswith(":") else DEFAULT_PORT
        host = host_part
    elif ":" in authority:
        host, _, port_str = authority.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Invalid port in target: {target_str!r}") from None
    else:
        host = authority
        port = DEFAULT_PORT

    if not host:
        raise ValueError(f"Missing host in target: {target_str!r}")
    if not (1 <= port <= 65535):
        raise ValueError(f"Port out of range (1..65535) in target: {target_str!r}")

    return host, port, clean_path


class Client:
    """Persistent HBIN/1 client communicating over a single TCP connection."""

    def __init__(self, host: str, port: int, verbose: bool = False, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.timeout = timeout
        self.next_stream_id = 1

        # Establish single TCP connection
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.rfile: BinaryIO = self.sock.makefile("rb")
        self.wfile: BinaryIO = self.sock.makefile("wb")

    def close(self):
        """Close connection streams and socket."""
        for stream in (self.wfile, self.rfile):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _log_frame(self, direction: str, frame: wire.Frame, extra_info: str = ""):
        """Hexdump frame if verbose mode is enabled."""
        if not self.verbose:
            return

        type_name = {
            wire.T_REQUEST: "REQUEST",
            wire.T_RESPONSE: "RESPONSE",
            wire.T_DATA: "DATA",
        }.get(frame.type, f"TYPE_{frame.type}")

        flags_desc = "END_STREAM" if frame.end_stream else "NONE"
        hdr = (
            f"\n* {direction} frame: type={type_name} (0x{frame.type:02x}), "
            f"flags=0x{frame.flags:02x} [{flags_desc}], "
            f"stream={frame.stream_id}, len={len(frame.payload)}"
        )
        if extra_info:
            hdr += f" ({extra_info})"
        print(hdr, file=sys.stderr)

        raw_bytes = frame.encode()
        for line in hexdump(raw_bytes):
            print(line, file=sys.stderr)

    def fetch(self, path: str, method: int = wire.M_GET, extra_headers: list[tuple[str, str]] | None = None) -> tuple[int, list[tuple[str, str]], bytes]:
        """Execute request and return (status, headers, body) over existing connection."""
        stream_id = self.next_stream_id
        self.next_stream_id += 1

        # Construct request headers
        host_header = f"{self.host}:{self.port}" if self.port != 80 else self.host
        req_headers = [
            ("host", host_header),
            ("user-agent", USER_AGENT),
            ("accept", "*/*"),
        ]
        if extra_headers:
            req_headers.extend(extra_headers)

        # Build and encode REQUEST frame with END_STREAM
        req_payload = wire.encode_request(method, path, req_headers)
        req_frame = wire.Frame(wire.T_REQUEST, wire.F_END_STREAM, stream_id, req_payload)

        # Transmit request
        self._log_frame(">>> SEND", req_frame, f"{wire.METHOD_NAMES.get(method, 'REQ')} {path}")
        self.wfile.write(req_frame.encode())
        self.wfile.flush()

        status: int | None = None
        resp_headers: list[tuple[str, str]] = []
        body_parts: list[bytes] = []
        stream_ended = False

        # Read frames until END_STREAM is seen for this stream
        while not stream_ended:
            frame = wire.read_frame(self.rfile)
            if frame is None:
                raise ProtocolError("Server closed connection prematurely before END_STREAM")

            self._log_frame("<<< RECV", frame)

            # Rule: skip unknown frame types cleanly
            if frame.type not in wire.KNOWN_TYPES:
                if self.verbose:
                    print(f"* Skipped unrecognized frame type 0x{frame.type:02x}", file=sys.stderr)
                continue

            # Frame must belong to active stream
            if frame.stream_id != stream_id:
                raise ProtocolError(f"Received frame for stream {frame.stream_id}, expected {stream_id}")

            if frame.type == wire.T_RESPONSE:
                if status is not None:
                    raise ProtocolError("Received duplicate RESPONSE frame on stream")
                status, resp_headers = wire.decode_response(frame.payload)
                if frame.end_stream:
                    stream_ended = True

            elif frame.type == wire.T_DATA:
                if status is None:
                    raise ProtocolError("Received DATA frame before RESPONSE frame")
                body_parts.append(frame.payload)
                if frame.end_stream:
                    stream_ended = True

            elif frame.type == wire.T_REQUEST:
                raise ProtocolError("Server sent illegal REQUEST frame")

        if status is None:
            raise ProtocolError("Stream ended without receiving RESPONSE frame")

        full_body = b"".join(body_parts)
        return status, resp_headers, full_body


def main(argv: list[str] | None = None) -> int:
    """Run bcurl CLI command."""
    if argv is None:
        argv = sys.argv[1:]

    verbose = False
    is_head = False
    targets = []

    # Parse arguments
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in ("-v", "--verbose"):
            verbose = True
        elif arg in ("-I", "--head"):
            is_head = True
        elif arg.startswith("-"):
            print(f"Unknown flag: {arg}", file=sys.stderr)
            print("Usage: ./bcurl [-v] [-I] host[:port]/path [path2 ...]", file=sys.stderr)
            return EXIT_USAGE_ERROR
        else:
            targets.append(arg)
        idx += 1

    if not targets:
        print("Usage: ./bcurl [-v] [-I] host[:port]/path [path2 ...]", file=sys.stderr)
        print("Example: ./bcurl -v localhost:9000/index.html", file=sys.stderr)
        return EXIT_USAGE_ERROR

    first_target = targets[0]
    try:
        host, port, first_path = parse_target(first_target)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    paths_to_fetch = [first_path]

    # Additional targets reuse the same connection host/port
    for extra in targets[1:]:
        if ":" in extra or extra.startswith("http://"):
            try:
                h, p, pth = parse_target(extra)
                if (h, p) != (host, port):
                    print(f"Error: All targets must share the same server connection ({host}:{port}) to fulfill single connection requirement.", file=sys.stderr)
                    return EXIT_USAGE_ERROR
                paths_to_fetch.append(pth)
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                return EXIT_USAGE_ERROR
        else:
            clean_p = extra if extra.startswith("/") else "/" + extra
            paths_to_fetch.append(clean_p)

    method = wire.M_HEAD if is_head else wire.M_GET
    worst_status = EXIT_SUCCESS

    try:
        # Single connection for all requested resources
        with Client(host, port, verbose=verbose) as client:
            for pth in paths_to_fetch:
                if verbose:
                    print(f"* Connecting to {host}:{port} -> Fetching {pth}", file=sys.stderr)

                status, headers, body = client.fetch(pth, method=method)

                if verbose:
                    print(f"* Status: {status}", file=sys.stderr)
                    for k, v in headers:
                        print(f"< {k}: {v}", file=sys.stderr)
                    print(f"* Body bytes: {len(body)}\n", file=sys.stderr)

                # Output body to stdout
                sys.stdout.buffer.write(body)
                sys.stdout.buffer.flush()

                # Requirement: exit non-zero on 4xx / 5xx
                if status >= 400:
                    worst_status = EXIT_HTTP_ERROR

    except (socket.error, OSError) as err:
        print(f"bcurl network error: {err}", file=sys.stderr)
        return EXIT_NETWORK_ERROR
    except (wire.WireError, ProtocolError) as err:
        print(f"bcurl protocol error: {err}", file=sys.stderr)
        return EXIT_PROTOCOL_ERROR

    return worst_status


if __name__ == "__main__":
    sys.exit(main())

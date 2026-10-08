"""HBIN/1 wire format: frame encoding/decoding and header blocks.

Pure binary codec for frames and payloads as defined in SPEC.md.
"""

from dataclasses import dataclass
from typing import BinaryIO

# Frame layout constants
FRAME_HEADER_LEN = 8
MAX_PAYLOAD = 16384
MAX_PATH = 8192
MAX_VALUE = 0xFFFF
MAX_STREAM_ID = 0xFFFFFF

# Frame types
T_REQUEST = 0x01
T_RESPONSE = 0x02
T_DATA = 0x03
KNOWN_TYPES = (T_REQUEST, T_RESPONSE, T_DATA)

# Frame flags
F_END_STREAM = 0x01

# Methods
M_GET = 1
M_HEAD = 2
METHOD_NAMES = {M_GET: "GET", M_HEAD: "HEAD"}

# Static header table (Section 4)
HEADER_NAMES = (
    "host",
    "user-agent",
    "accept",
    "if-none-match",
    "server",
    "content-type",
    "content-length",
    "last-modified",
    "etag",
    "allow",
)
_NAME_TO_ID = {name: idx for idx, name in enumerate(HEADER_NAMES, start=1)}
_ID_TO_NAME = {idx: name for name, idx in _NAME_TO_ID.items()}


class WireError(Exception):
    """Base error for binary wire protocol issues."""


class Truncated(WireError):
    """The connection terminated unexpectedly mid-frame."""


class FrameTooLarge(WireError):
    """Frame payload exceeds MAX_PAYLOAD limit (16384 bytes)."""

    def __init__(self, length: int, stream_id: int):
        super().__init__(f"frame length {length} exceeds maximum payload {MAX_PAYLOAD}")
        self.length = length
        self.stream_id = stream_id


class Malformed(WireError):
    """Frame was received completely but payload violates protocol rules."""


@dataclass(frozen=True)
class Frame:
    type: int
    flags: int
    stream_id: int
    payload: bytes = b""

    @property
    def end_stream(self) -> bool:
        return bool(self.flags & F_END_STREAM)

    def encode(self) -> bytes:
        return encode_frame(self.type, self.flags, self.stream_id, self.payload)


# --- Frame Encoding & Decoding (Section 2) ---

def encode_frame(type_: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    """Encode an 8-byte frame header and payload."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload size {len(payload)} exceeds max limit {MAX_PAYLOAD}")
    if not (0 <= stream_id <= MAX_STREAM_ID):
        raise ValueError(f"stream id {stream_id} must fit in 24 bits (0..{MAX_STREAM_ID})")

    # Length (3B) + Type (1B) + Flags (1B) + Stream ID (3B) = 8 bytes
    header = (
        len(payload).to_bytes(3, "big")
        + bytes([type_ & 0xFF, flags & 0xFF])
        + stream_id.to_bytes(3, "big")
    )
    return header + payload


def _read_exact(rfile: BinaryIO, count: int) -> bytes:
    """Read exactly count bytes from a file-like stream or raise Truncated."""
    chunks = []
    read_so_far = 0
    while read_so_far < count:
        part = rfile.read(count - read_so_far)
        if not part:
            raise Truncated(f"expected {count} bytes, connection closed after {read_so_far}")
        chunks.append(part)
        read_so_far += len(part)
    return b"".join(chunks)


def read_frame(rfile: BinaryIO) -> Frame | None:
    """Read a single frame from stream.

    Returns None on clean EOF at frame boundary.
    Raises Truncated if EOF occurs mid-frame.
    Raises FrameTooLarge if header indicates payload > MAX_PAYLOAD.
    """
    header = rfile.read(FRAME_HEADER_LEN)
    if not header:
        return None
    if len(header) < FRAME_HEADER_LEN:
        raise Truncated(f"incomplete header: read {len(header)} of {FRAME_HEADER_LEN} bytes")

    length = int.from_bytes(header[0:3], "big")
    frame_type = header[3]
    flags = header[4]
    stream_id = int.from_bytes(header[5:8], "big")

    if length > MAX_PAYLOAD:
        raise FrameTooLarge(length, stream_id)

    payload = _read_exact(rfile, length) if length > 0 else b""
    return Frame(frame_type, flags, stream_id, payload)


# --- Header Block Encoding & Decoding (Section 4) ---

def encode_headers(headers: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> bytes:
    """Encode headers using indexed table or literal format."""
    out = bytearray()
    for name, value in headers:
        normalized_name = name.lower()
        encoded_value = value.encode("utf-8")
        if len(encoded_value) > MAX_VALUE:
            raise ValueError(f"header value for '{name}' exceeds {MAX_VALUE} bytes")

        index = _NAME_TO_ID.get(normalized_name)
        if index is not None:
            # Indexed: id:u8 (1..10) + value_len:u16 + value
            out.append(index)
        else:
            # Literal: 0x00 + name_len:u8 + name:ascii + value_len:u16 + value
            ascii_name = normalized_name.encode("ascii")
            if not (1 <= len(ascii_name) <= 255):
                raise ValueError(f"invalid literal header name '{name}'")
            out.append(0x00)
            out.append(len(ascii_name))
            out.extend(ascii_name)

        out.extend(len(encoded_value).to_bytes(2, "big"))
        out.extend(encoded_value)

    return bytes(out)


def decode_headers(buf: bytes) -> list[tuple[str, str]]:
    """Decode header block. Safely skips unknown indexed header IDs (Section 4)."""
    headers = []
    offset = 0
    total_len = len(buf)

    def read_bytes(n: int) -> bytes:
        nonlocal offset
        if offset + n > total_len:
            raise Malformed("header entry runs past payload boundary")
        data = buf[offset:offset + n]
        offset += n
        return data

    while offset < total_len:
        index = read_bytes(1)[0]
        if index == 0:
            # Literal header
            name_len = read_bytes(1)[0]
            if name_len == 0:
                raise Malformed("literal header has zero-length name")
            try:
                name = read_bytes(name_len).decode("ascii").lower()
            except UnicodeDecodeError:
                raise Malformed("literal header name is not valid ASCII") from None
        else:
            # Indexed header (or unknown index 11..255)
            name = _ID_TO_NAME.get(index)

        value_len = int.from_bytes(read_bytes(2), "big")
        raw_val = read_bytes(value_len)

        # If header ID was unknown (11..255), specification requires skipping it
        if name is None:
            continue

        try:
            value = raw_val.decode("utf-8")
        except UnicodeDecodeError:
            raise Malformed(f"header value for '{name}' is not valid UTF-8") from None

        headers.append((name, value))

    return headers


# --- REQUEST & RESPONSE Payloads (Section 3) ---

def encode_request(method: int, path: str, headers: list[tuple[str, str]] | tuple[tuple[str, str], ...] = ()) -> bytes:
    """Encode REQUEST frame payload."""
    raw_path = path.encode("utf-8")
    if len(raw_path) > MAX_PATH:
        raise ValueError(f"path length {len(raw_path)} exceeds max {MAX_PATH}")
    return bytes([method]) + len(raw_path).to_bytes(2, "big") + raw_path + encode_headers(headers)


def decode_request(payload: bytes) -> tuple[int, str, list[tuple[str, str]]]:
    """Decode REQUEST payload into (method, path, headers)."""
    if len(payload) < 3:
        raise Malformed("REQUEST payload too short for fixed fields")

    method = payload[0]
    path_len = int.from_bytes(payload[1:3], "big")

    if path_len > MAX_PATH:
        raise Malformed(f"path length {path_len} exceeds maximum allowed {MAX_PATH}")
    if 3 + path_len > len(payload):
        raise Malformed("path length extends beyond payload boundary")

    try:
        path = payload[3:3 + path_len].decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("path is not valid UTF-8") from None

    if not path.startswith("/"):
        raise Malformed("request path must start with '/'")
    if "\0" in path:
        raise Malformed("request path contains NUL byte")

    headers = decode_headers(payload[3 + path_len:])
    return method, path, headers


def encode_response(status: int, headers: list[tuple[str, str]] | tuple[tuple[str, str], ...] = ()) -> bytes:
    """Encode RESPONSE frame payload."""
    return status.to_bytes(2, "big") + encode_headers(headers)


def decode_response(payload: bytes) -> tuple[int, list[tuple[str, str]]]:
    """Decode RESPONSE payload into (status, headers)."""
    if len(payload) < 2:
        raise Malformed("RESPONSE payload too short for status code")
    status = int.from_bytes(payload[:2], "big")
    headers = decode_headers(payload[2:])
    return status, headers

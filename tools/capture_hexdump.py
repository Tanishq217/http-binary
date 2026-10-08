"""Tool to capture and verify reference HBIN/1 request and response frames."""

from hbin import wire
from hbin.hexdump import hexdump


def capture_example() -> dict[str, bytes]:
    """Generate deterministic wire frames matching the reference exchange in docs/HEXDUMP.md."""
    # 1. Request frame: GET /index.html on stream 1
    req_payload = wire.encode_request(wire.M_GET, "/index.html", [
        ("host", "localhost:9000"),
        ("user-agent", "bcurl/1"),
        ("accept", "*/*"),
    ])
    req_frame = wire.encode_frame(wire.T_REQUEST, wire.F_END_STREAM, 1, req_payload)

    # 2. Response frame: 200 OK with 5 standard headers
    resp_payload = wire.encode_response(200, [
        ("server", "bserve/1"),
        ("content-type", "text/html; charset=utf-8"),
        ("content-length", "15"),
        ("last-modified", "Tue, 14 Nov 2023 22:13:20 GMT"),
        ("etag", '"f-17979cfe362a0000"'),
    ])
    resp_frame = wire.encode_frame(wire.T_RESPONSE, 0, 1, resp_payload)

    # 3. Data frame: 15 bytes body
    body = b"<h1>hello</h1>\n"
    data_frame = wire.encode_frame(wire.T_DATA, wire.F_END_STREAM, 1, body)

    # 4. Unknown frame (Type 0x55, Stream 0, 5-byte payload)
    unknown_frame = wire.encode_frame(0x55, 0, 0, b"hello")

    return {
        "request": req_frame,
        "response": resp_frame,
        "data": data_frame,
        "unknown": unknown_frame,
    }


def main():
    frames = capture_example()
    for name, raw in frames.items():
        print(f"=== {name.upper()} ({len(raw)} bytes) ===")
        for line in hexdump(raw):
            print(line)
        print()


if __name__ == "__main__":
    main()

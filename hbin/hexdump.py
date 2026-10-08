"""Hexdump utility formatted in standard hexdump -C layout."""


def hexdump(data: bytes, limit: int | None = None) -> list[str]:
    """Format bytes into lines of standard 16-byte hex dump with ASCII sidebar.

    Optionally truncates display at `limit` bytes with a summary line.
    """
    shown = data if limit is None else data[:limit]
    lines = []

    for offset in range(0, len(shown), 16):
        chunk = shown[offset:offset + 16]
        left_bytes = " ".join(f"{b:02x}" for b in chunk[:8])
        right_bytes = " ".join(f"{b:02x}" for b in chunk[8:])
        ascii_text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{offset:08x}  {left_bytes:<23}  {right_bytes:<23}  |{ascii_text}|")

    if limit is not None and len(data) > limit:
        lines.append(f"... ({len(data)} bytes total, truncated at {limit})")

    return lines

"""Verification test ensuring docs/HEXDUMP.md matches reference protocol frames."""

import os
import re
import unittest

from tools.capture_hexdump import capture_example

DOC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "HEXDUMP.md")
HEX_LINE_RE = re.compile(r"^[0-9a-f]{8}  (([0-9a-f]{2} ){1,8}) (([0-9a-f]{2} ?){1,8})")


def extract_hexdump_blocks(markdown_text: str) -> list[bytes]:
    """Parse hexdump code blocks in markdown and reconstruct raw bytes."""
    blocks = []
    current_block = bytearray()
    in_block = False

    for line in markdown_text.splitlines():
        # Match lines like: 00000000  00 00 2f 01 01 00 00 01  01 00 0b 2f 69 6e 64 65  |../......../inde|
        if re.match(r"^[0-9a-f]{8}  ", line):
            in_block = True
            hex_part = line[10:58].strip()
            hex_bytes = bytes.fromhex(hex_part.replace(" ", ""))
            current_block.extend(hex_bytes)
        else:
            if in_block and current_block:
                blocks.append(bytes(current_block))
                current_block = bytearray()
                in_block = False

    if in_block and current_block:
        blocks.append(bytes(current_block))

    return blocks


class HexdumpDocVerificationTests(unittest.TestCase):
    def test_doc_matches_actual_wire_frames(self):
        with open(DOC_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        blocks = extract_hexdump_blocks(content)
        self.assertGreaterEqual(len(blocks), 4, "Expected at least 4 hexdump blocks in docs/HEXDUMP.md")

        ref = capture_example()

        # Block 0: REQUEST frame
        self.assertEqual(blocks[0], ref["request"], "Documented REQUEST frame does not match wire output")

        # Block 1: RESPONSE frame
        self.assertEqual(blocks[1], ref["response"], "Documented RESPONSE frame does not match wire output")

        # Block 2: DATA frame
        self.assertEqual(blocks[2], ref["data"], "Documented DATA frame does not match wire output")

        # Block 3: Unknown frame
        self.assertEqual(blocks[3], ref["unknown"], "Documented Unknown frame does not match wire output")


if __name__ == "__main__":
    unittest.main()

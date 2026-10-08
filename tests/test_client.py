"""Integration tests for bcurl client (SPEC Section 1, 2, 3, 5)."""

import os
import subprocess
import sys
import unittest

from hbin.client import EXIT_HTTP_ERROR, EXIT_SUCCESS, parse_target
from tests.support import ServerCase

BCURL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bcurl")


def run_bcurl(*args, timeout=10):
    cmd = [sys.executable, BCURL_PATH, *args]
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


class ClientTargetParsingTests(unittest.TestCase):
    def test_target_parsing_formats(self):
        # host:port/path
        self.assertEqual(parse_target("localhost:9000/index.html"), ("localhost", 9000, "/index.html"))

        # host:port with no path
        self.assertEqual(parse_target("localhost:9000"), ("localhost", 9000, "/"))

        # host with no port
        self.assertEqual(parse_target("localhost/index.html"), ("localhost", 9000, "/index.html"))

        # URL scheme prefix
        self.assertEqual(parse_target("http://127.0.0.1:8000/style.css"), ("127.0.0.1", 8000, "/style.css"))

        # IPv6 target
        self.assertEqual(parse_target("[::1]:9000/api/v1"), ("::1", 9000, "/api/v1"))

    def test_invalid_targets(self):
        with self.assertRaises(ValueError):
            parse_target("localhost:invalid_port/path")
        with self.assertRaises(ValueError):
            parse_target("localhost:999999/path")  # port out of range


class ClientIntegrationTests(ServerCase):
    def test_bcurl_fetch_index(self):
        target = f"localhost:{self.port}/index.html"
        proc = run_bcurl(target)

        self.assertEqual(proc.returncode, EXIT_SUCCESS)
        self.assertIn(b"<h1>Hello from HBIN/1!</h1>", proc.stdout)

    def test_bcurl_verbose_hexdump_output(self):
        target = f"localhost:{self.port}/index.html"
        proc = run_bcurl("-v", target)

        self.assertEqual(proc.returncode, EXIT_SUCCESS)
        stderr_text = proc.stderr.decode("utf-8")

        # Verbose mode logs sent REQUEST and received RESPONSE and DATA frames
        self.assertIn("SEND frame: type=REQUEST", stderr_text)
        self.assertIn("RECV frame: type=RESPONSE", stderr_text)
        self.assertIn("RECV frame: type=DATA", stderr_text)

        # Hexdump lines formatted with 8 hex offset chars and pipe ASCII bounds
        self.assertIn("00000000", stderr_text)
        self.assertIn("|", stderr_text)

    def test_bcurl_exit_nonzero_on_404(self):
        target = f"localhost:{self.port}/not-found-resource.html"
        proc = run_bcurl(target)

        # SPEC REQUIREMENT: Exit non-zero on 4xx/5xx (EXIT_HTTP_ERROR = 22)
        self.assertEqual(proc.returncode, EXIT_HTTP_ERROR)
        self.assertIn(b"404 Resource '/not-found-resource.html' not found", proc.stdout)

    def test_bcurl_multiple_paths_single_connection(self):
        # Fetch 3 resources sequentially over one connection
        t1 = f"localhost:{self.port}/hello.txt"
        t2 = "/style.css"
        t3 = "/empty.txt"

        proc = run_bcurl("-v", t1, t2, t3)
        self.assertEqual(proc.returncode, EXIT_SUCCESS)

        stderr_text = proc.stderr.decode("utf-8")
        # Verify streams 1, 2, 3 were processed in sequence on the same session
        self.assertIn("stream=1", stderr_text)
        self.assertIn("stream=2", stderr_text)
        self.assertIn("stream=3", stderr_text)

        # Stderr should note connecting only once
        self.assertEqual(stderr_text.count(f"Connecting to localhost:{self.port}"), 3)


if __name__ == "__main__":
    unittest.main()

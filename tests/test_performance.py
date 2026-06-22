"""Performance / load smoke tests.

Conservative, CI-safe guards: correctness under concurrency (a free-threading
race/deadlock detector), large-file streaming integrity, and a latency-regression
guard for the TCP_NODELAY fix (without it, small responses incur a ~40 ms
Nagle/delayed-ACK stall each).
"""

from __future__ import annotations

import http.client
import tempfile
import threading
import time
import unittest
from pathlib import Path

from servery.config import Config
from tests._harness import serving

_SMALL = b"x" * 1024
_LARGE_SIZE = 8 * 1024 * 1024


class PerfSmokeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "small.txt").write_bytes(_SMALL)
        (root / "big.bin").write_bytes(b"x" * _LARGE_SIZE)
        self.cfg = Config.create(root, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_small_responses_not_nagle_stalled(self):
        # 200 sequential keep-alive requests must be fast. With Nagle on, each
        # small response stalls ~40 ms (~8 s total); with TCP_NODELAY, well under.
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=10)
            start = time.perf_counter()
            for _ in range(200):
                conn.request("GET", "/small.txt")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                resp.read()
            elapsed = time.perf_counter() - start
            conn.close()
        self.assertLess(elapsed, 3.0, f"200 small requests took {elapsed:.2f}s (Nagle regression?)")

    def test_concurrent_load_is_correct(self):
        # 40 concurrent clients x 10 requests: every response must be correct.
        # Exercises the threaded/free-threaded path for races and deadlocks.
        errors: list[str] = []
        lock = threading.Lock()

        def worker(host: str, port: int) -> None:
            conn = http.client.HTTPConnection(host, port, timeout=10)
            problems: list[str] = []
            for _ in range(10):
                conn.request("GET", "/small.txt")
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200 or body != _SMALL:
                    problems.append(f"status={resp.status} len={len(body)}")
            conn.close()
            with lock:
                errors.extend(problems)

        with serving(self.cfg) as (host, port):
            threads = [threading.Thread(target=worker, args=(host, port)) for _ in range(40)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])

    def test_large_file_streaming_integrity(self):
        with serving(self.cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request("GET", "/big.bin")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(body), _LARGE_SIZE)


if __name__ == "__main__":
    unittest.main()

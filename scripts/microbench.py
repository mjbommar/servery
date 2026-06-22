#!/usr/bin/env python3
"""Single-core, CPU-bound microbenchmark for attributing per-request changes.

A synchronous (non-threaded) server handles requests in the main thread while one
keep-alive client drives them, so the number reflects per-request CPU cost with
low variance. Use for pre/post comparison of optimizations.

    python scripts/microbench.py            # default scenarios
    python scripts/microbench.py --n 8000
"""

from __future__ import annotations

import argparse
import http.client
import statistics
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from servery.config import Config
from servery.server import ServeryHTTPServer


class _SyncServer(ServeryHTTPServer):
    """Handle each request synchronously in the serve_forever thread."""

    def process_request(self, request, client_address):  # type: ignore[override]
        self.finish_request(request, client_address)
        self.shutdown_request(request)


def _measure(root: Path, path: str, n: int, warmup: int) -> float:
    server = _SyncServer(Config.create(root, host="127.0.0.1", port=0, quiet=True))
    host, port = server.server_address[0], server.server_address[1]
    result: dict[str, float] = {}

    def drive() -> None:
        conn = http.client.HTTPConnection(str(host), int(port), timeout=30)
        for _ in range(warmup):
            conn.request("GET", path)
            conn.getresponse().read()
        start = time.perf_counter()
        for _ in range(n):
            conn.request("GET", path)
            conn.getresponse().read()
        result["elapsed"] = time.perf_counter() - start
        conn.close()
        server.shutdown()

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    server.serve_forever(poll_interval=0.005)
    server.server_close()
    thread.join(timeout=5)
    return n / result["elapsed"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=6000)
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "small.txt").write_bytes(b"x" * 1024)
        for i in range(50):
            (root / f"file{i:02d}.txt").write_text("entry")

        scenarios = [("small file (1 KiB)", "/small.txt"), ("listing (50)", "/")]
        print(f"{'scenario':<22}{'req/s (median of ' + str(args.repeat) + ')':>26}")
        for label, path in scenarios:
            runs = [_measure(root, path, args.n, args.warmup) for _ in range(args.repeat)]
            print(f"{label:<22}{statistics.median(runs):>20.0f} req/s")


if __name__ == "__main__":
    main()

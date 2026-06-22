#!/usr/bin/env python3
"""Benchmark servery: throughput + latency for file serving (pure stdlib).

Starts an in-process server, hammers it with concurrent http.client workers, and
reports requests/sec, MB/sec, and latency percentiles for a small file, a large
file (exercises the sendfile path), and a directory listing.

    python scripts/bench.py --requests 5000 --concurrency 16
"""

from __future__ import annotations

import argparse
import http.client
import statistics
import tempfile
import threading
import time
from pathlib import Path

from servery.config import Config
from servery.server import make_server


def _bench(host: str, port: int, path: str, requests: int, concurrency: int) -> dict[str, float]:
    latencies: list[float] = []
    byte_total = 0
    errors = 0
    lock = threading.Lock()
    per_worker = max(1, requests // concurrency)

    def worker() -> None:
        nonlocal byte_total, errors
        conn = http.client.HTTPConnection(host, port, timeout=30)
        local_lat: list[float] = []
        local_bytes = 0
        local_err = 0
        for _ in range(per_worker):
            start = time.perf_counter()
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    local_err += 1
                local_bytes += len(body)
            except OSError:
                local_err += 1
                conn = http.client.HTTPConnection(host, port, timeout=30)
                continue
            local_lat.append(time.perf_counter() - start)
        with lock:
            latencies.extend(local_lat)
            byte_total += local_bytes
            errors += local_err

    workers = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.perf_counter()
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    elapsed = time.perf_counter() - started

    ordered = sorted(latencies)
    count = len(ordered) or 1
    return {
        "requests": len(latencies),
        "errors": errors,
        "rps": len(latencies) / elapsed if elapsed else 0.0,
        "mb_s": byte_total / 1e6 / elapsed if elapsed else 0.0,
        "p50_ms": statistics.median(ordered) * 1000 if ordered else 0.0,
        "p99_ms": ordered[min(count - 1, int(count * 0.99))] * 1000 if ordered else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=4000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--large-mib", type=int, default=50)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "small.txt").write_bytes(b"x" * 1024)
        (root / "large.bin").write_bytes(b"x" * (args.large_mib * 1024 * 1024))
        for i in range(50):
            (root / f"file{i}.txt").write_text("listing entry")

        httpd = make_server(Config.create(root, host="127.0.0.1", port=0, quiet=True))
        host, port = httpd.server_address[0], httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            scenarios = [
                ("small file (1 KiB)", "/small.txt", args.requests, args.concurrency),
                (
                    "large file (sendfile)",
                    "/large.bin",
                    max(64, args.concurrency * 4),
                    args.concurrency,
                ),
                ("directory listing", "/", args.requests, args.concurrency),
            ]
            print(
                f"{'scenario':<26}{'req/s':>10}{'MB/s':>10}{'p50 ms':>10}{'p99 ms':>10}{'err':>6}"
            )
            for label, path, n, c in scenarios:
                result = _bench(str(host), int(port), path, n, c)
                print(
                    f"{label:<26}{result['rps']:>10.0f}{result['mb_s']:>10.1f}"
                    f"{result['p50_ms']:>10.2f}{result['p99_ms']:>10.2f}{result['errors']:>6}"
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()

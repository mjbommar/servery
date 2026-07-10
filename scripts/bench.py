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
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from servery.config import Config
from servery.server import make_server


def _rss_bytes() -> int:
    """Best-effort current RSS for the in-process server + load generator."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError, AttributeError):
        try:
            import resource

            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(value if sys.platform == "darwin" else value * 1024)
        except (ImportError, ValueError):  # pragma: no cover - Windows fallback
            return 0


def _bench(
    host: str, port: int, path: str, requests: int, concurrency: int
) -> dict[str, float | int]:
    latencies: list[float] = []
    byte_total = 0
    errors = 0
    lock = threading.Lock()
    per_worker, remainder = divmod(requests, concurrency)
    baseline_rss = _rss_bytes()
    peak_rss = baseline_rss
    stop_sampling = threading.Event()

    def sample_memory() -> None:
        nonlocal peak_rss
        while not stop_sampling.wait(0.01):
            peak_rss = max(peak_rss, _rss_bytes())

    def consume(response: http.client.HTTPResponse) -> int:
        """Drain one response without making the load client buffer it as one object."""
        received = 0
        while chunk := response.read(64 * 1024):
            received += len(chunk)
        return received

    def worker(operations: int) -> None:
        nonlocal byte_total, errors
        conn = http.client.HTTPConnection(host, port, timeout=30)
        try:
            # Warm up so one scenario's cache/memory state doesn't taint the next.
            for _ in range(min(50, operations)):
                conn.request("GET", path)
                consume(conn.getresponse())
            local_lat: list[float] = []
            local_bytes = 0
            local_err = 0
            for _ in range(operations):
                start = time.perf_counter()
                try:
                    conn.request("GET", path)
                    resp = conn.getresponse()
                    if resp.status != 200:
                        local_err += 1
                    local_bytes += consume(resp)
                except OSError:
                    local_err += 1
                    conn.close()
                    conn = http.client.HTTPConnection(host, port, timeout=30)
                    continue
                local_lat.append(time.perf_counter() - start)
            with lock:
                latencies.extend(local_lat)
                byte_total += local_bytes
                errors += local_err
        finally:
            conn.close()

    workers = [
        threading.Thread(target=worker, args=(per_worker + (index < remainder),))
        for index in range(concurrency)
    ]
    sampler = threading.Thread(target=sample_memory, name="servery-bench-rss", daemon=True)
    sampler.start()
    started = time.perf_counter()
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    elapsed = time.perf_counter() - started
    stop_sampling.set()
    sampler.join(timeout=1)
    peak_rss = max(peak_rss, _rss_bytes())

    ordered = sorted(latencies)

    def percentile(q: float) -> float:
        if not ordered:
            return 0.0
        return ordered[max(0, math.ceil(len(ordered) * q) - 1)] * 1000

    return {
        "requests": len(latencies),
        "errors": errors,
        "rps": len(latencies) / elapsed if elapsed else 0.0,
        "mb_s": byte_total / 1e6 / elapsed if elapsed else 0.0,
        "p50_ms": statistics.median(ordered) * 1000 if ordered else 0.0,
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "peak_rss_mib": peak_rss / 1024 / 1024,
        "rss_delta_mib": max(0, peak_rss - baseline_rss) / 1024 / 1024,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=4000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--large-mib", type=int, default=50)
    parser.add_argument("--json", type=Path, help="write machine-readable evidence to PATH")
    args = parser.parse_args()
    if args.requests <= 0 or args.concurrency <= 0 or args.large_mib <= 0:
        parser.error("--requests, --concurrency, and --large-mib must be positive")

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
                f"{'scenario':<26}{'req/s':>10}{'MB/s':>10}{'p50 ms':>10}"
                f"{'p95 ms':>10}{'p99 ms':>10}{'RSS Δ':>10}{'err':>6}"
            )
            scenario_evidence: dict[str, object] = {}
            evidence: dict[str, object] = {
                "generated_at": datetime.now(UTC).isoformat(),
                "python": sys.version,
                "concurrency": args.concurrency,
                "requested_operations": args.requests,
                "large_file_mib": args.large_mib,
                "memory_scope": "in-process server plus client workers",
                "scenarios": scenario_evidence,
            }
            for label, path, n, c in scenarios:
                result = _bench(str(host), int(port), path, n, c)
                result["concurrency"] = c
                scenario_evidence[label] = result
                print(
                    f"{label:<26}{result['rps']:>10.0f}{result['mb_s']:>10.1f}"
                    f"{result['p50_ms']:>10.2f}{result['p95_ms']:>10.2f}"
                    f"{result['p99_ms']:>10.2f}{result['rss_delta_mib']:>10.1f}"
                    f"{result['errors']:>6}"
                )
            if args.json is not None:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                args.json.write_text(json.dumps(evidence, indent=2) + "\n")
                print(f">> wrote {args.json}")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()

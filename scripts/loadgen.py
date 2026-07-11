#!/usr/bin/env python3
"""Out-of-process async HTTP load generator (keep-alive, Content-Length framed).

Unlike ``bench.py`` (in-process), this hits a server running in a *separate*
process over loopback, so it measures real server throughput. The client is
asyncio (cheap concurrency) and can fan out across processes to saturate a
many-core server.

    # in one shell: python -m servery /some/dir -p 8000 -q
    python scripts/loadgen.py http://127.0.0.1:8000/file.txt -c 64 -d 5 --procs 4
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import multiprocessing
import random
import statistics
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


class _LatencySampler:
    """Keep all observations or a deterministic uniform reservoir."""

    __slots__ = ("_random", "max_samples", "samples", "seen")

    def __init__(self, max_samples: int | None, seed: int) -> None:
        self.max_samples = max_samples
        self.samples: list[float] = []
        self.seen = 0
        self._random = random.Random(seed)

    def add(self, value: float) -> None:
        self.seen += 1
        if self.max_samples is None or len(self.samples) < self.max_samples:
            self.samples.append(value)
            return
        replacement = self._random.randrange(self.seen)
        if replacement < self.max_samples:
            self.samples[replacement] = value


@dataclass(frozen=True, slots=True)
class LoadCohort:
    """One independently reported cohort in a synchronized mixed workload."""

    name: str
    url: str
    concurrency: int
    procs: int = 1
    close: bool = False
    expected_status: int = 200
    request_headers: tuple[tuple[str, str], ...] = ()
    request_body_size: int = 0
    read_chunk_size: int | None = None
    read_delay: float = 0.0


async def _delayed_worker(
    delay: float, worker: Callable[..., Awaitable[None]], *args: object
) -> None:
    """Start one connection worker after an optional ramp delay."""
    if delay:
        await asyncio.sleep(delay)
    await worker(*args)


async def _read_response(
    reader: asyncio.StreamReader,
    *,
    read_chunk_size: int | None = None,
    read_delay: float = 0.0,
) -> tuple[int, int, bool]:
    """Read one Content-Length-framed response; return status, length, and close intent."""
    head = await reader.readuntil(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0].split()
    if len(first) < 2 or not first[1].isdigit():
        raise ValueError("malformed HTTP status line")
    status = int(first[1])
    length = 0
    framed = False
    should_close = False
    for line in head.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        if name.lower() == b"content-length":
            length = int(line.split(b":", 1)[1])
            framed = True
        elif name.lower() == b"connection" and b"close" in value.lower():
            should_close = True
    if not framed and (100 <= status < 200 or status in {204, 304}):
        framed = True
    if not framed:
        raise ValueError("comparison load generator requires Content-Length")
    if length and read_chunk_size is None:
        await reader.readexactly(length)
    elif length and read_chunk_size is not None:
        remaining = length
        while remaining:
            chunk = await reader.readexactly(min(read_chunk_size, remaining))
            remaining -= len(chunk)
            if remaining and read_delay:
                await asyncio.sleep(read_delay)
    return status, length, should_close


async def _keepalive(
    host: str,
    port: int,
    request: bytes,
    measurement_start: float,
    deadline: float,
    latencies: _LatencySampler,
    counters: list[int],
    status_counts: dict[int, int],
    interval_counts: dict[int, int],
    expected_status: int,
    read_chunk_size: int | None,
    read_delay: float,
) -> None:
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=min(5.0, remaining),
            )
        except (OSError, TimeoutError):
            if time.monotonic() >= measurement_start:
                counters[2] += 1
                counters[4] += 1
            await asyncio.sleep(0)
            continue
        try:
            while time.monotonic() < deadline:
                t0 = time.monotonic()
                writer.write(request)
                await writer.drain()
                status, length, should_close = await _read_response(
                    reader,
                    read_chunk_size=read_chunk_size,
                    read_delay=read_delay,
                )
                if t0 >= measurement_start:
                    status_counts[status] = status_counts.get(status, 0) + 1
                    if status != expected_status:
                        counters[2] += 1
                        counters[3] += 1
                    counters[1] += length
                    completed_at = time.monotonic()
                    latencies.add(completed_at - t0)
                    counters[0] += 1
                    interval = max(0, int(completed_at - measurement_start))
                    interval_counts[interval] = interval_counts.get(interval, 0) + 1
                if should_close:
                    break
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if time.monotonic() >= measurement_start:
                counters[2] += 1
                counters[4] += 1
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def _one_close_request(
    host: str,
    port: int,
    request: bytes,
    measured: bool,
    counters: list[int],
    status_counts: dict[int, int],
    expected_status: int,
    read_chunk_size: int | None,
    read_delay: float,
) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(request)
        await writer.drain()
        status, length, _should_close = await _read_response(
            reader,
            read_chunk_size=read_chunk_size,
            read_delay=read_delay,
        )
        if measured:
            status_counts[status] = status_counts.get(status, 0) + 1
            if status != expected_status:
                counters[2] += 1
                counters[3] += 1
            counters[1] += length
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _churn(
    host: str,
    port: int,
    request: bytes,
    measurement_start: float,
    deadline: float,
    latencies: _LatencySampler,
    counters: list[int],
    status_counts: dict[int, int],
    interval_counts: dict[int, int],
    expected_status: int,
    read_chunk_size: int | None,
    read_delay: float,
) -> None:
    """A fresh connection per request (Connection: close) — stresses accept/backlog.

    Each cycle is bounded by a timeout so a connect/read stalled by a full backlog
    can't outlive the run (the loop only re-checks the deadline between cycles).
    """
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        measured = t0 >= measurement_start
        try:
            await asyncio.wait_for(
                _one_close_request(
                    host,
                    port,
                    request,
                    measured,
                    counters,
                    status_counts,
                    expected_status,
                    read_chunk_size,
                    read_delay,
                ),
                timeout=5.0,
            )
        except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
            if measured:
                counters[2] += 1  # refused/reset/stalled (backlog overflow shows here)
                counters[4] += 1
            continue
        if measured:
            completed_at = time.monotonic()
            latencies.add(completed_at - t0)
            counters[0] += 1
            interval = max(0, int(completed_at - measurement_start))
            interval_counts[interval] = interval_counts.get(interval, 0) + 1


async def _run_async(
    host: str,
    port: int,
    path: str,
    conns: int,
    warmup: float,
    connection_ramp: float,
    duration: float,
    close: bool,
    expected_status: int,
    request_headers: tuple[tuple[str, str], ...],
    request_body_size: int,
    read_chunk_size: int | None,
    read_delay: float,
    max_latency_samples: int | None,
    sample_seed: int,
) -> dict:
    conn_header = "close" if close else "keep-alive"
    extra_headers = "".join(f"{name}: {value}\r\n" for name, value in request_headers)
    method = "POST" if request_body_size else "GET"
    content_length = f"Content-Length: {request_body_size}\r\n" if request_body_size else ""
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: {conn_header}\r\n"
        f"{content_length}{extra_headers}\r\n"
    ).encode("latin-1") + b"u" * request_body_size
    latencies = _LatencySampler(max_latency_samples, sample_seed)
    counters = [0, 0, 0, 0, 0]  # requests, bytes, all/status/transport errors
    status_counts: dict[int, int] = {}
    interval_counts: dict[int, int] = {}
    started = time.monotonic()
    measurement_start = started + warmup
    deadline = measurement_start + duration
    worker = _churn if close else _keepalive
    tasks = [
        asyncio.create_task(
            _delayed_worker(
                connection_ramp * index / conns,
                worker,
                host,
                port,
                request,
                measurement_start,
                deadline,
                latencies,
                counters,
                status_counts,
                interval_counts,
                expected_status,
                read_chunk_size,
                read_delay,
            )
        )
        for index in range(conns)
    ]
    if warmup:
        await asyncio.sleep(warmup)
    cpu_started = time.process_time()
    await asyncio.gather(*tasks)
    return {
        "requests": counters[0],
        "bytes": counters[1],
        "errors": counters[2],
        "status_errors": counters[3],
        "transport_errors": counters[4],
        "status_counts": status_counts,
        "interval_counts": interval_counts,
        "latencies": latencies.samples,
        "latencies_seen": latencies.seen,
        "elapsed": time.monotonic() - measurement_start,
        "client_cpu_s": time.process_time() - cpu_started,
    }


def _worker(args: tuple) -> dict:
    (
        host,
        port,
        path,
        conns,
        warmup,
        connection_ramp,
        duration,
        close,
        expected_status,
        headers,
        body_size,
        chunk_size,
        delay,
        max_latency_samples,
        sample_seed,
    ) = args
    return asyncio.run(
        _run_async(
            host,
            port,
            path,
            conns,
            warmup,
            connection_ramp,
            duration,
            close,
            expected_status,
            headers,
            body_size,
            chunk_size,
            delay,
            max_latency_samples,
            sample_seed,
        )
    )


def run_load(
    url: str,
    *,
    concurrency: int = 64,
    warmup: float = 0.0,
    connection_ramp: float = 0.0,
    duration: float = 5.0,
    procs: int = 1,
    close: bool = False,
    expected_status: int = 200,
    request_headers: tuple[tuple[str, str], ...] = (),
    request_body_size: int = 0,
    read_chunk_size: int | None = None,
    read_delay: float = 0.0,
    max_latency_samples: int | None = None,
) -> dict[str, object]:
    """Drive one URL and return a machine-readable throughput/latency summary.

    ``max_latency_samples`` bounds retained observations across the final result.
    Counts and throughput remain exact; latency statistics then use a deterministic
    stratified reservoir. ``None`` preserves the historical all-samples behavior.
    """
    if concurrency <= 0 or duration <= 0 or procs <= 0:
        raise ValueError("concurrency, duration, and procs must be positive")
    if warmup < 0:
        raise ValueError("warmup cannot be negative")
    if connection_ramp < 0:
        raise ValueError("connection_ramp cannot be negative")
    if procs > concurrency:
        raise ValueError("procs cannot exceed concurrency")
    if read_chunk_size is not None and read_chunk_size <= 0:
        raise ValueError("read_chunk_size must be positive")
    if read_delay < 0:
        raise ValueError("read_delay cannot be negative")
    if request_body_size < 0:
        raise ValueError("request_body_size cannot be negative")
    if max_latency_samples is not None and max_latency_samples <= 0:
        raise ValueError("max_latency_samples must be positive")
    for name, value in request_headers:
        if not name or ":" in name or "\r" in name or "\n" in name:
            raise ValueError("request header names must be non-empty HTTP field names")
        if "\r" in value or "\n" in value:
            raise ValueError("request header values cannot contain CR or LF")
        try:
            name.encode("ascii")
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError("request headers must be HTTP/1-compatible text") from exc
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError("loadgen currently supports plaintext http URLs only")
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    per_proc, remainder = divmod(concurrency, procs)
    jobs = [
        (
            host,
            port,
            path,
            per_proc + (index < remainder),
            warmup,
            connection_ramp,
            duration,
            close,
            expected_status,
            request_headers,
            request_body_size,
            read_chunk_size,
            read_delay,
            max_latency_samples,
            index,
        )
        for index in range(procs)
    ]
    if procs == 1:
        results = [_worker(jobs[0])]
    else:
        with multiprocessing.Pool(procs) as pool:
            results = pool.map(_worker, jobs)

    elapsed = max(float(result["elapsed"]) for result in results)
    total_req = sum(int(result["requests"]) for result in results)
    total_bytes = sum(int(result["bytes"]) for result in results)
    total_err = sum(int(result["errors"]) for result in results)
    status_errors = sum(int(result["status_errors"]) for result in results)
    transport_errors = sum(int(result["transport_errors"]) for result in results)
    status_counts: dict[str, int] = {}
    interval_counts: dict[str, int] = {}
    for result in results:
        for status, count in result["status_counts"].items():
            key = str(status)
            status_counts[key] = status_counts.get(key, 0) + int(count)
        for interval, count in result["interval_counts"].items():
            key = str(interval)
            interval_counts[key] = interval_counts.get(key, 0) + int(count)
    client_cpu_s = sum(float(result["client_cpu_s"]) for result in results)
    latencies_seen = sum(int(result["latencies_seen"]) for result in results)
    if max_latency_samples is None or latencies_seen <= max_latency_samples:
        lat = sorted(float(value) for result in results for value in result["latencies"])
    else:
        # Allocate the final reservoir in proportion to each process's completed
        # requests. Each process retained up to the global cap, so its local
        # reservoir always has enough observations for its assigned quota.
        exact_quotas = [
            max_latency_samples * int(result["latencies_seen"]) / latencies_seen
            for result in results
        ]
        quotas = [math.floor(quota) for quota in exact_quotas]
        remaining = max_latency_samples - sum(quotas)
        order = sorted(
            range(len(results)),
            key=lambda index: (exact_quotas[index] - quotas[index], -index),
            reverse=True,
        )
        for index in order[:remaining]:
            quotas[index] += 1
        combined: list[float] = []
        for index, (result, quota) in enumerate(zip(results, quotas, strict=True)):
            local = [float(value) for value in result["latencies"]]
            if quota < len(local):
                local = random.Random(10_000 + index).sample(local, quota)
            combined.extend(local)
        lat = sorted(combined)

    def pct(p: float) -> float:
        if not lat:
            return 0.0
        return lat[max(0, math.ceil(len(lat) * p) - 1)] * 1000

    return {
        "url": url,
        "mode": "close" if close else "keepalive",
        "concurrency": concurrency,
        "client_processes": procs,
        "duration_s": duration,
        "warmup_s": warmup,
        "connection_ramp_s": connection_ramp,
        "elapsed_s": elapsed,
        "requests": total_req,
        "bytes": total_bytes,
        "errors": total_err,
        "status_errors": status_errors,
        "transport_errors": transport_errors,
        "status_counts": status_counts,
        "completion_intervals": interval_counts,
        "client_cpu_s": client_cpu_s,
        "client_cpu_cores": client_cpu_s / elapsed if elapsed else 0.0,
        "rps": total_req / elapsed if elapsed else 0.0,
        "mb_s": total_bytes / elapsed / 1e6 if elapsed else 0.0,
        "p50_ms": pct(0.50),
        "p90_ms": pct(0.90),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "mean_ms": statistics.mean(lat) * 1000 if lat else 0.0,
        "latency_samples_seen": latencies_seen,
        "latency_samples_retained": len(lat),
        "latency_sampling": "all" if max_latency_samples is None else "reservoir-stratified",
        "max_latency_samples": max_latency_samples,
    }


def run_mixed_load(
    cohorts: tuple[LoadCohort, ...],
    *,
    warmup: float = 0.0,
    connection_ramp: float = 0.0,
    duration: float = 5.0,
    max_latency_samples: int | None = None,
) -> dict[str, object]:
    """Run two or more cohorts concurrently and keep their results separate.

    Each cohort owns an event loop (and optional client processes).  A barrier
    releases all cohort controllers together, avoiding the sequential-load error
    where an expensive workload has already ended before the protected cheap
    workload starts.  Results are intentionally not folded into aggregate RPS.
    """
    if len(cohorts) < 2:
        raise ValueError("mixed load requires at least two cohorts")
    names = [cohort.name for cohort in cohorts]
    if any(not name for name in names):
        raise ValueError("cohort names cannot be empty")
    if len(set(names)) != len(names):
        raise ValueError("cohort names must be unique")
    barrier = threading.Barrier(len(cohorts))

    def run(cohort: LoadCohort) -> dict[str, object]:
        barrier.wait()
        return run_load(
            cohort.url,
            concurrency=cohort.concurrency,
            warmup=warmup,
            connection_ramp=connection_ramp,
            duration=duration,
            procs=cohort.procs,
            close=cohort.close,
            expected_status=cohort.expected_status,
            request_headers=cohort.request_headers,
            request_body_size=cohort.request_body_size,
            read_chunk_size=cohort.read_chunk_size,
            read_delay=cohort.read_delay,
            max_latency_samples=max_latency_samples,
        )

    started = time.monotonic()
    with ThreadPoolExecutor(
        max_workers=len(cohorts),
        thread_name_prefix="servery-load-cohort",
    ) as executor:
        futures = {cohort.name: executor.submit(run, cohort) for cohort in cohorts}
        results = {name: future.result() for name, future in futures.items()}
    return {
        "mode": "mixed",
        "duration_s": duration,
        "warmup_s": warmup,
        "connection_ramp_s": connection_ramp,
        "wall_s": time.monotonic() - started,
        "cohorts": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("-c", "--concurrency", type=int, default=64, help="total connections")
    parser.add_argument("-d", "--duration", type=float, default=5.0, help="seconds")
    parser.add_argument(
        "--warmup", type=float, default=0.0, help="untimed persistent warmup seconds"
    )
    parser.add_argument(
        "--connection-ramp",
        type=float,
        default=0.0,
        help="seconds over which each client process staggers connection starts",
    )
    parser.add_argument("--procs", type=int, default=1, help="client processes")
    parser.add_argument(
        "--close", action="store_true", help="new connection per request (stresses accept/backlog)"
    )
    parser.add_argument("--expect-status", type=int, default=200)
    parser.add_argument("--request-body-size", type=int, default=0)
    parser.add_argument("--read-chunk-size", type=int)
    parser.add_argument("--read-delay", type=float, default=0.0)
    parser.add_argument(
        "--max-latency-samples",
        type=int,
        help="cap retained latency observations; counts and throughput remain exact",
    )
    parser.add_argument("--json", type=Path, help="write the summary to PATH")
    args = parser.parse_args()
    try:
        result = run_load(
            args.url,
            concurrency=args.concurrency,
            warmup=args.warmup,
            connection_ramp=args.connection_ramp,
            duration=args.duration,
            procs=args.procs,
            close=args.close,
            expected_status=args.expect_status,
            request_body_size=args.request_body_size,
            read_chunk_size=args.read_chunk_size,
            read_delay=args.read_delay,
            max_latency_samples=args.max_latency_samples,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(f"url            {result['url']}")
    print(f"mode           {result['mode']}")
    print(f"concurrency    {args.concurrency} conns / {args.procs} proc(s)")
    print(
        f"requests       {result['requests']}  in {result['elapsed_s']:.2f}s   "
        f"errors={result['errors']}"
    )
    print(f"throughput     {result['rps']:,.0f} req/s   {result['mb_s']:,.1f} MB/s")
    if result["requests"]:
        print(
            f"latency ms     p50={result['p50_ms']:.2f}  p90={result['p90_ms']:.2f}  "
            f"p99={result['p99_ms']:.2f}  mean={result['mean_ms']:.2f}"
        )
        if result["latency_sampling"] != "all":
            print(
                f"latency sample {result['latency_samples_retained']:,} / "
                f"{result['latency_samples_seen']:,} observations "
                f"({result['latency_sampling']})"
            )
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()

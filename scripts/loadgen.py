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
import multiprocessing
import statistics
import time
import urllib.parse


async def _read_response(reader: asyncio.StreamReader) -> int:
    """Read one Content-Length-framed HTTP/1.1 response; return body length."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line[:15].lower() == b"content-length:":
            length = int(line.split(b":", 1)[1])
            break
    if length:
        await reader.readexactly(length)
    return length


async def _keepalive(
    host: str,
    port: int,
    request: bytes,
    deadline: float,
    latencies: list[float],
    counters: list[int],
) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            writer.write(request)
            await writer.drain()
            counters[1] += await _read_response(reader)
            latencies.append(time.monotonic() - t0)
            counters[0] += 1
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _one_close_request(host: str, port: int, request: bytes, counters: list[int]) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(request)
        await writer.drain()
        counters[1] += await _read_response(reader)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _churn(
    host: str,
    port: int,
    request: bytes,
    deadline: float,
    latencies: list[float],
    counters: list[int],
) -> None:
    """A fresh connection per request (Connection: close) — stresses accept/backlog.

    Each cycle is bounded by a timeout so a connect/read stalled by a full backlog
    can't outlive the run (the loop only re-checks the deadline between cycles).
    """
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(_one_close_request(host, port, request, counters), timeout=5.0)
        except (OSError, asyncio.IncompleteReadError, TimeoutError):
            counters[2] += 1  # refused/reset/stalled (backlog overflow shows here)
            continue
        latencies.append(time.monotonic() - t0)
        counters[0] += 1


async def _run_async(
    host: str, port: int, path: str, conns: int, duration: float, close: bool
) -> dict:
    conn_header = "close" if close else "keep-alive"
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: {conn_header}\r\n\r\n").encode(
        "latin-1"
    )
    latencies: list[float] = []
    counters = [0, 0, 0]  # [requests, bytes, errors]
    deadline = time.monotonic() + duration
    worker = _churn if close else _keepalive
    await asyncio.gather(
        *(worker(host, port, request, deadline, latencies, counters) for _ in range(conns)),
        return_exceptions=True,
    )
    return {
        "requests": counters[0],
        "bytes": counters[1],
        "errors": counters[2],
        "latencies": latencies,
    }


def _worker(args: tuple) -> dict:
    host, port, path, conns, duration, close = args
    return asyncio.run(_run_async(host, port, path, conns, duration, close))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("-c", "--concurrency", type=int, default=64, help="total connections")
    parser.add_argument("-d", "--duration", type=float, default=5.0, help="seconds")
    parser.add_argument("--procs", type=int, default=1, help="client processes")
    parser.add_argument(
        "--close", action="store_true", help="new connection per request (stresses accept/backlog)"
    )
    args = parser.parse_args()

    parsed = urllib.parse.urlsplit(args.url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    per_proc = max(1, args.concurrency // args.procs)
    job = (host, port, path, per_proc, args.duration, args.close)
    t0 = time.monotonic()
    if args.procs == 1:
        results = [_worker(job)]
    else:
        with multiprocessing.Pool(args.procs) as pool:
            results = pool.map(_worker, [job] * args.procs)
    elapsed = time.monotonic() - t0

    total_req = sum(r["requests"] for r in results)
    total_bytes = sum(r["bytes"] for r in results)
    total_err = sum(r["errors"] for r in results)
    lat = sorted(ms for r in results for ms in r["latencies"])
    rps = total_req / elapsed

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))] * 1000 if lat else 0.0

    mode = "close (churn)" if args.close else "keep-alive"
    print(f"url            {args.url}")
    print(f"mode           {mode}")
    print(f"concurrency    {args.concurrency} conns / {args.procs} proc(s)")
    print(f"requests       {total_req}  in {elapsed:.2f}s   errors={total_err}")
    print(f"throughput     {rps:,.0f} req/s   {total_bytes / elapsed / 1e6:,.1f} MB/s")
    if lat:
        print(
            f"latency ms     p50={pct(0.50):.2f}  p90={pct(0.90):.2f}  "
            f"p99={pct(0.99):.2f}  mean={statistics.mean(lat) * 1000:.2f}"
        )


if __name__ == "__main__":
    main()

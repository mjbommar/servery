# Benchmarks

servery ships a reproducible benchmark suite built on
[pytest-benchmark](https://pytest-benchmark.readthedocs.io/). It measures **per-request
latency** for every transport (HTTP/1.1, TLS, HTTP/2 ALPN + h2c, HTTP/3, WSGI, CGI,
ASGI, reverse proxy) plus the internal hot paths (HPACK, the frame codec, listing
rendering, Range parsing, the response-head builder, cert generation).

The functional test suite is `unittest` (run with `python -m unittest`); pytest is used
**only** here, scoped to `benchmarks/` via `testpaths` so the two never collide.

## Running

```bash
# all benchmarks (default free-threaded 3.14t interpreter)
uv run --group bench pytest benchmarks/

# one file / one case
uv run --group bench pytest benchmarks/test_bench_http1.py
uv run --group bench pytest benchmarks/ -k wsgi

# save a reproducible artifact + autosave for later comparison
scripts/run_benchmarks.sh

# fail if any median regressed >20% vs the last saved run
scripts/run_benchmarks.sh --compare

# also run the HTTP/3 end-to-end case (needs a GIL build + aioquic; see below)
scripts/run_benchmarks.sh --http3
```

`run_benchmarks.sh` writes a timestamped JSON to `benchmarks/artifacts/` and an autosave
under `.benchmarks/` (both gitignored — they embed per-round timings and the hostname).

## HTTP/3 note

`aioquic` (the QUIC stack) has no free-threaded build, so it can't be imported on the
default 3.14t interpreter. HTTP/3 is therefore covered in two layers:

- `test_http3_build_response_*` — the servery-owned request→response path (path safety,
  file/listing read, header assembly). Pure stdlib, **always runs**.
- `test_http3_end_to_end` — a real QUIC/H3 round-trip via aioquic over one persistent
  connection. `importorskip`-gated; run it on a GIL build:

  ```bash
  uv run --python 3.13 --group bench --extra http3 pytest benchmarks/test_bench_http3.py -k end_to_end
  ```

## Reference numbers

Single keep-alive connection, single thread (so this is **latency**, not peak throughput),
on a free-threaded CPython 3.14t build (GIL off). Your absolute numbers will differ; the
*ratios* are the durable signal.

### Transports — per request

| Transport | Client | Median | ops/s |
|---|---|---:|---:|
| WSGI (bare app) | http.client | ~47 µs | ~19.5k |
| ASGI (bare app) | http.client | ~49 µs | ~19.4k |
| HTTP/1.1, 1 KiB file | http.client | ~89 µs | ~10.4k |
| HTTP/2 cleartext (h2c) | httpx | ~288 µs | ~3.1k |
| HTTP/1.1 over TLS, 1 KiB | httpx | ~274 µs | ~3.2k |
| HTTP/2 ALPN (over TLS) | httpx | ~331 µs | ~2.8k |
| Reverse proxy (→ upstream) | http.client | ~386 µs | ~2.5k |
| HTTP/3 e2e (QUIC, on 3.13) | aioquic | ~380 µs | ~2.4k |
| HTTP/1.1, 8 MiB (sendfile) | http.client | ~1.69 ms | ~570 (≈4.9 GB/s) |
| CGI (fork + exec) | http.client | ~11.9 ms | ~84 |

### Internal hot paths — per call

| Path | Median | ops/s |
|---|---:|---:|
| `_http1.build_head` | ~0.84 µs | ~1.17M |
| frame serialize (DATA) | ~0.99 µs | ~1.0M |
| `ranges.parse` | ~1.10 µs | ~894k |
| frame parse (DATA) | ~1.54 µs | ~638k |
| HPACK encode (request) | ~3.46 µs | ~286k |
| HPACK decode (request) | ~4.23 µs | ~235k |
| `http3.build_response` (file) | ~17.4 µs | ~57k |
| `listing.render` (52 entries) | ~354 µs | ~2.8k |
| `_certgen.generate` (RSA-2048) | ~0.5–0.9 s | ~1–2 |

## Reading the results — what the numbers say

- **WSGI and ASGI are the fastest** request paths (~19k ops/s) — no filesystem, tiny
  body — and ASGI's asyncio loop matches the threaded WSGI path on a single connection.
- **The TLS/HTTP-2/HTTP-3 rows are driven by httpx/aioquic**, which add real client-side
  cost; compare those rows *to each other*, not to the `http.client`-driven plain rows.
  Among them: TLS h1 ≈ h2c > h2-ALPN ≈ h3 — TLS framing on a single stream is cheaper
  than h2/h3's multiplexing machinery when you're not actually multiplexing.
- **CGI is ~250× slower than WSGI** — it forks and execs a fresh interpreter per request.
  That's inherent to CGI; the benchmark shows the cost rather than hiding it. Use WSGI/ASGI
  for anything hot.
- **Large-file serving is sendfile-bound** (~4.9 GB/s on loopback), not CPU-bound.
- **HPACK decode is the slowest HTTP/2 hot path** (~4.2 µs/request) — the place to look
  first if h2 header throughput ever matters.
- **`listing.render` dominates directory serving** (~354 µs for 52 entries). Profiling
  shows the time is spread across necessary work — per-row HTML building, one `stat` and
  one `localtime` per entry, URL-quoting and HTML-escaping each field — with no redundant
  pass to remove. It's already tuned (an `lru_cache` on extension parsing, hand-rolled
  time formatting to avoid `strftime`/`datetime` allocation). Cost scales with entry count
  and is capped at 100k entries per request (the scan DoS guard).
- **`--tls-self-signed` costs ~0.5–0.9 s once at startup** — pure-Python RSA-2048 key
  generation. This is the price of zero runtime dependencies (there's no OpenSSL keygen
  binding to call). It's one-time per process and never persisted; a faster EC dev key
  would need a pure-Python ECDSA implementation (possible future work). For a long-running
  server it's noise; for rapid restart-driven dev loops it's noticeable.

## Throughput (concurrent) vs latency (this suite)

pytest-benchmark measures single-call latency. For **concurrent throughput** (req/s under
load, MB/s, tail latency) on the HTTP/1.1 file path, use the load tools:

```bash
python scripts/bench.py --requests 5000 --concurrency 16        # in-process
python -m servery /some/dir -p 8000 -q                          # one shell
python scripts/loadgen.py http://127.0.0.1:8000/file -c 64 -d 5 # another shell
```

# Selector compression and transient single-flight — 2026-07-10

Status: accept transient same-key compression single-flight in the shared
production cache. Accept compression as a benchmark-only selector capability
with explicit worker, queue, input-size, and retained-cache budgets. Do not expose
a public selector backend from these results.

## Problem and design boundary

Compression is unlike warm metadata lookup: reading and encoding a bounded file
is CPU and memory work that must not run on the selector event loop. It also
changes the representation ETag, framing, range policy, and body ownership. The
prototype therefore reuses rather than reimplements:

- `_static.open_file` for coding selection and the coding-specific strong ETag;
- `_compress.CompressionCache` and its canonical path/mtime/size/coding/level key;
- the existing rule that `Range` forces identity and coded responses do not
  advertise `Accept-Ranges`;
- the existing `max_compress_size` input cap and byte-bounded retained cache.

The selector adds a separate compression worker/queue budget because filesystem
latency and compression CPU are different operator resources. Compression is
disabled unless workers are configured. Cache hits stay on the event loop;
distinct misses enter the bounded executor or receive immediate `503` at
saturation.

For an uncached large input, the worker owns a duplicated descriptor for the
already-opened identity. Connection cancellation may close the original handle
without racing the worker. The duplicate is closed by the worker on hit, success,
or failure. In-flight futures are owned through shutdown.

## Single-flight revision

The first external run exposed an avoidable production asymmetry. Selector
requests for the same key shared one asyncio future, but
`CompressionCache(max_bytes=0)` made every production thread encode separately.
The initial uncached result was 15.57k selector RPS versus 4.72k production RPS.

The accepted cache now has transient per-key flights independent of retention:

- concurrent callers for one key receive one immutable encoded result;
- after the last concurrent caller returns, a zero-budget cache retains zero
  bytes and the next request computes again;
- different keys compute concurrently under the caller's worker/thread budget;
- errors reach every waiter and the flight is reclaimed;
- enabled-cache hits still return before any flight bookkeeping.

This replaces the previous global enabled-cache compute lock, which also
serialized unrelated files. The flight table is reference-counted and bounded by
the number of concurrent distinct misses, not historical key cardinality.

## Correctness and failure gates

Direct tests cover:

- deterministic gzip bytes, `Content-Encoding`, exact coded length, `Vary`, and
  coding-specific ETag;
- coding-correct `304`, bodyless coded HEAD, identity `206`, and identity `416`;
- warm retained-cache hits and same-key transient sharing with retention off;
- distinct-key concurrency in the shared cache;
- bounded distinct-key saturation, immediate `503`, and recovery;
- cancellation while a large duplicated descriptor is owned by a worker;
- atomic path replacement still encoding the original opened identity;
- in-place truncation producing `500` rather than a coded body with stale facts;
- worker errors reaching all waiters and flight cleanup.

The benchmark client validates the exact deterministic gzip body hash plus
`Content-Encoding: gzip` and `Vary: Accept-Encoding` before each timed sample.

## Fair benchmark cohorts

Two capability-scoped 64 KiB gzip scenarios use CPython 3.15.0b3 with the GIL,
one server CPU, two isolated client processes, 64 keep-alive connections, seven
rotated three-second trials, and zero errors:

- warm cache: production and selector each retain up to 32 MiB; selector uses one
  worker plus 64 bounded queue slots;
- sustained uncached: retained budget is zero in both; selector retains the same
  one-worker/64-queue resource ceiling. Same-key concurrent work may be shared
  transiently in both implementations.

The source is deliberately compressible, so these are request/encoding policy
tests rather than network-bandwidth tests. Client CPU remained far below its
saturation threshold.

## Warm-cache result

| Server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| production | 15.94k | 5.5% | 16.77 ms | 30.9 MiB |
| pre-change production | 16.18k | 0.6% | 17.04 ms | 30.8 MiB |
| selector | 16.15k | 5.4% | 4.59 ms | 27.5 MiB |

Production versus its paired baseline is -1.1% RPS / -0.8% p99, with 6.0%/4.9%
ratio MAD: warm-cache behavior is neutral. Selector/production within-trial RPS
is +6.3% with 8.5% MAD and a -13.8% to +19.6% range, so no throughput direction
is claimed. Selector p99 is 72.2% lower with 2.5% ratio MAD.

## Uncached result

| Server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| production with transient single-flight | 9.47k | 1.8% | 31.29 ms | 50.8 MiB |
| pre-change production | 4.86k | 1.4% | 42.76 ms | 51.1 MiB |
| selector | 15.76k | 2.5% | 6.61 ms | 27.2 MiB |

The production paired result is **+91.2% RPS and -24.8% p99**, with 1.5% and
4.8% ratio MAD. This is an accepted material improvement even when retained
cache policy is zero. Peak cgroup memory is unchanged; single-flight removes
duplicate CPU/output work but does not remove the threaded connections and their
opened/input state.

Against improved production, selector within-trial results are +65.3% RPS
(2.8% MAD) and -78.7% p99 (0.3% MAD). The remaining gap is connection scheduling,
thread footprint, and executor ownership—not missing compression coalescing.

Ignored artifacts under `benchmarks/artifacts/`:

- `selector-compression-2026-07-10.json` (diagnostic first shape);
- `selector-compression-v2-2026-07-10.json` (warm and revised aggregate result);
- `selector-compression-miss-paired-2026-07-10.json` (decision-grade miss A/B).

## Configuration and decision

No new shipped setting is needed for production: `max_compress_size` remains the
per-input memory cap and `compression_cache_size` remains retained-byte policy.
Transient sharing does not turn a zero cache budget into retention.

A future selector backend would additionally need explicit compression workers
and queue slots; they must not be inferred from `max_connections` or hidden in a
global default executor. On the one-CPU benchmark, one worker is the honest
policy. Multi-CPU worker scaling remains a separate gate.

Keep the selector implementation experimental. Compression semantics and owned
miss work are now credible, but TLS/platform fallbacks, access logs, and feature
routing remain incomplete. Generated listings and representation digests were
addressed in the follow-on
[bounded listing](2026-07-10-selector-listings.md) and
[opened-identity digest](2026-07-10-selector-digests.md) experiments.

## Verification

- 768 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds; each run has four expected optional-integration skips.
- Repository-wide Ruff format/lint, ty, Bandit, strict MkDocs, and
  `git diff --check` gates pass.
- The wheel and source distribution build, the wheel declares zero runtime
  dependencies, and its installed `servery --version` runs outside the source
  tree.

# Selector frontend spike — 2026-07-10

Status: architecture signal confirmed; production backend not accepted. These
are machine-specific research results, not portable reference numbers.

## Hypothesis

The one-thread-per-connection frontend accounts for enough of the residual
small-static and connection-churn gap that a selector-based frontend deserves
further design work. A benchmark-only frontend should provide a ceiling estimate
before servery commits to a second production connection architecture.

## Prototype and scope

`benchmarks/comparison/selector_spike.py` is a 180-line asyncio streams server.
It reuses servery's safe containment, ETag, and HTTP-date helpers; uses the same
read-only corpus and response hash checks; performs a bounded 16 KiB read for
small files; and uses the event loop's `sendfile` integration for larger files.
It has no production configuration or CLI surface.

The spike is intentionally incomplete. It omits ranges, conditionals,
compression, directories, uploads, WebDAV, proxying, TLS, access logs, request
body framing, slow-client timeouts, overload/admission policy, and graceful
drain. Filesystem containment/open/stat work is synchronous on the event loop.
It is therefore a connection-model ceiling test, not a fair product replacement.

## Controls and correctness

- CPython 3.15.0b3 with the GIL, one server CPU, four isolated client CPUs,
  plaintext HTTP/1.1, warm cache, five five-second trials, and deterministic
  server-order rotation.
- Production servery and the spike ran from the same Docker image and mounted
  the same corpus read-only.
- The harness verified status, content length, and SHA-256 before each sample.
  Timed requests recorded zero errors.
- Median client CPU remained 26–62%, so none of these rows is marked
  client-limited.

Artifact: ignored local file `selector-spike-2026-07-10.json` under
`benchmarks/artifacts/`.

## Results

| Workload | Production servery | Selector spike | Change | Median peak memory |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns | 18.85k req/s, 14.15 ms p99 | 30.36k req/s, 2.71 ms p99 | +61.1% throughput, −80.8% p99 | 28.3 vs 26.1 MiB |
| 1 KiB churn, 32 conns | 6.16k req/s, 7.04 ms p99 | 10.92k req/s, 3.62 ms p99 | +77.2% throughput, −48.6% p99 | 27.7 vs 27.8 MiB |

Production/spike RPS MAD was 4.9%/5.7% for keep-alive and 2.1%/5.4% for
churn. The spike's churn maximum (14.84k) was an outlier relative to its 10.92k
median, which is why the median and MAD—not the best trial—are reported.

The result clears the roadmap's directional architecture target and confirms
that low-risk tuning alone is unlikely to close the residual gap. It does not
show that asyncio itself causes the entire gain: the spike also has a smaller
feature path and combines each small response head and body into one write. The
follow-up prototype now uses the same request-line and incremental header-block
rules as the threaded handler. Framing, `Expect`, pipelined-byte ownership,
timeouts, and the connection state machine remain spike-specific or absent; the
[shared-parser experiment](2026-07-10-shared-request-parser.md) records the
correctness and paired-performance gate.

### Size sweep

A three-trial follow-up crossed the 16 KiB buffered threshold into the spike's
event-loop `sendfile` path:

| Body/concurrency | Production servery | Selector spike | Interpretation |
| --- | ---: | ---: | --- |
| 16 KiB / 64 | 18.24k req/s, 16.18 ms p99 | 29.01k req/s, 2.85 ms p99 | +59.1%; the small buffered advantage persists |
| 64 KiB / 64 | 15.58k req/s, 16.58 ms p99 | 15.54k req/s, 5.12 ms p99 | throughput parity; substantially lower selector tail |
| 8 MiB / 16 | 2.89 GB/s, 61.45 ms p99 | 2.76 GB/s, 69.50 ms p99 | both clients at 100%; no valid ranking |

Artifact: ignored local file `selector-spike-size-sweep-2026-07-10.json` under
`benchmarks/artifacts/`. All responses were correct and error-free. The 64 KiB
result shows that eliminating connection threads can improve tail scheduling
without increasing zero-copy throughput; the client-limited 8 MiB row protects
against a dramatic regression but cannot establish parity.

## Isolating response write batching

A second, same-image five-trial experiment compared production's separate
header/body writes with a candidate that appended the bounded 1 KiB body to the
already-buffered response head.

| Workload | Separate writes | Coalesced write | Change |
| --- | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns | 20.89k req/s, 12.91 ms p99 | 21.99k req/s, 11.80 ms p99 | +5.2% throughput, −8.6% p99 |
| 1 KiB churn, 32 conns | 6.79k req/s, 6.83 ms p99 | 6.54k req/s, 6.84 ms p99 | −3.7% throughput, unchanged p99 |

Artifact: ignored local file `static-write-coalescing-2026-07-10.json` under
`benchmarks/artifacts/`.

Write batching explains only a small part of the keep-alive result and none of
the churn improvement. It misses the 10% acceptance threshold, adds special
response-state handling, and slightly regresses churn, so the candidate was
removed from production code. The public `small_file_buffer_size` threshold
continues to control the meaningful read-versus-sendfile resource tradeoff.

## Architecture decision

Decision: **continue the selector design, do not promote this spike.**

The next production-shaped checkpoint is now recorded in
[Production-shaped selector prototype — 2026-07-10](2026-07-10-selector-production-prototype.md).
It adds connection-state and failure-mode realism while keeping the original
spike intact as a ceiling control.

The performance signal is large enough to justify a production-shaped prototype,
but the missing feature and failure-mode surface is also large. Threaded remains
the default and only production HTTP/1 backend. No public `connection_backend`
setting should exist until another implementation can share request parsing and
response planning and pass the full HTTP/1 conformance corpus.

## Next design questions

1. Extract a shared request/response plan without creating two drifting HTTP
   implementations. The selector should own I/O state, not duplicate semantics.
2. Add bounded header parsing, idle/header/body/write deadlines, maximum requests
   per connection, admission limits, overload rejection, cancellation, and
   graceful drain before performance promotion.
3. Decide how blocking containment/open/stat and directory work leave the event
   loop. A generic executor can recreate queueing; OS-anchored lookup is not
   portable and Python 3.15 does not expose Linux `openat2`.
4. Prove partial-write/backpressure/sendfile behavior with slow readers, aborted
   transfers, ranges, file truncation/replacement, and high concurrency.
5. Evaluate Windows proactor and macOS selector behavior separately. The asyncio
   API is portable, but zero-copy and readiness details are not equivalent.
6. Keep dynamic WSGI/CGI and write-heavy features on the threaded backend unless
   shared semantics and measured benefit justify migration. ASGI already owns an
   asyncio runtime and should not be routed through a second loop.

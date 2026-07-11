# Bounded selector access logging — 2026-07-11

## Question

Can the benchmark-only selector frontend provide production-compatible access
records without blocking its event loop, growing memory without limit, or hiding
overload by silently discarding records?

The existing threaded HTTP/1 server formats and writes a record synchronously at
`end_headers`. That is a reasonable design when each request already owns a
thread: the file handler serializes writes, and a slow log destination applies
backpressure to those request threads. Calling the same writer from an asyncio
connection task would instead block every connection sharing that event loop.
Moving writes to an unbounded background queue would only exchange event-loop
blocking for an unbounded-memory failure mode.

## Accepted prototype shape

The selector prototype now owns one dedicated access-log writer thread. A
capacity token is acquired before an immutable record enters the handoff queue;
the total budget is one active write plus `access_log_queue` waiting records.
The queue's storage primitive is unbounded internally, but no insertion occurs
without a token, so accepted memory is bounded by policy rather than by timing.

Two overload policies are deliberately separate:

- `drop` uses a nonblocking token acquisition. A full budget increments
  `access_log_dropped` and lets the response proceed. This preserves availability
  but is not an audit-completeness policy.
- `wait` asynchronously waits for a token. It never blocks the event-loop thread,
  but queue saturation delays response headers and therefore applies honest log
  backpressure.

The record is accepted immediately before response headers are written, matching
the production handler's timing. A client abort after headers may therefore
still have a record; a request that times out before any response does not. File,
directory, redirect, conditional, range, method, parser, saturation, and internal
error responses all use the same path. `Content-Length` is captured when present;
bodyless responses such as `304` retain `-`, matching production semantics.

Shutdown first stops and drains connection tasks. The writer then waits for every
accepted queue task, receives a terminal sentinel, joins, and closes the log.
Writer failures do not suppress responses; they increment
`access_log_errors`. Unit tests cover exact JSON facts, GET/HEAD/404 sizes,
referer and user-agent fields, deterministic saturation drops, async wait
backpressure with an event-loop ticker, graceful drain, and injected write
failure.

## Batch policy and tradeoff

The first implementation submitted one executor future per record. It was
correct but expensive: allocation plus event-loop completion callbacks cost more
than the file write. A persistent queue consumer removed the future allocation,
but the writer still usually received one record per scheduler turn.

The accepted research control therefore makes both batch dimensions explicit:

- `access_log_batch_size=8` bounds one combined flush;
- `access_log_batch_wait=1 ms` gives the writer a short window to fill that
  batch after receiving its first record.

A zero wait restores immediate collection. The wait is log-durability latency,
not response latency: connection tasks wait only for queue admission. Larger
batches or windows may improve throughput but retain more accepted records across
a process crash and delay their visibility to tailing tools. These settings are
prototype policy, not shipped CLI/configuration.

`AccessLog.format_line()` and `AccessLog.write_lines()` now expose the existing
formatter and one locked multi-line handler flush to transport adapters. The
ordinary production `record()` method still writes one record. A five-trial
same-image-shape A/B against the immediately preceding implementation measured
the production one-record refactor at +1.8% median paired RPS and -5.9% p99;
ratio MADs were 7.3 and 6.4 percentage points, so both effects are neutral.

## Fairness controls

The capability-scoped `static-access-log-1k` scenario uses the same 1 KiB body,
one server CPU, 64 persistent connections, two client processes, one-second
warmup, three-second timed trials, and three rotated trials for:

- current and frozen production, logged and unlogged;
- the unlogged selector prototype;
- selector logging with `drop` and `wait` policies.

The harness waits for warmup logging to quiesce and truncates the file before
timing. After timing, it waits for the log line count to stabilize and records
`access_log_delivery_pct` against completed requests. The count is out of band.
Cgroup memory is captured before the audit helper enters the container, avoiding
a measured 6 MiB helper-process contamination found in an earlier run. Exact
body/status probes and response-error accounting remain mandatory.

The reported artifact is
`benchmarks/artifacts/selector-access-log-final-2026-07-11.json` (gitignored).
It records CPython 3.15.0b3 with the GIL, candidate image ID
`sha256:587b8eff...`, frozen baseline image ID `sha256:48180c4c...`, and product
tree hash `278baa94...`.

## Final one-CPU results

| adapter | median RPS | RPS MAD | p99 | peak MiB | log delivery |
|---|---:|---:|---:|---:|---:|
| production, unlogged | 18,924 | 3.5% | 15.48 ms | 30.1 | n/a |
| frozen production, unlogged | 18,073 | 5.8% | 16.86 ms | 30.0 | n/a |
| selector, unlogged | 18,078 | 4.6% | 4.47 ms | 26.1 | n/a |
| production, logged | 12,337 | 1.2% | 6.33 ms | 34.3 | 100% |
| frozen production, logged | 12,967 | 5.3% | 6.28 ms | 34.0 | 100% |
| selector, drop | 13,335 | 1.2% | 6.14 ms | 33.1 | 100% |
| selector, wait | 13,239 | 11.1% | 5.72 ms | 33.1 | 100% |

Against current logged production, selector `drop` is 8.1% higher in aggregate
median RPS, has 3.1% lower p99, and uses 1.2 MiB less peak memory. Selector
`wait` is 7.3% higher in RPS, 9.7% lower in p99, and uses 1.2 MiB less, but its
11.1% RPS MAD makes the drop result the stronger signal.
All three trials delivered 100% of completed-request records for all logged
adapters, so the drop result received no throughput credit from omissions.

Architecture-local logged/unlogged per-trial ratios remain noisy because server
orders differ within each rotation. Their medians put the logging cost at 34.8%
for current production, 29.5% for selector `drop`, and 30.0% for selector `wait`;
these are workload cost estimates, not stable rankings. The stronger claim is
the direct logged comparison above, where production and selector-drop RPS MAD
are both 1.2%.

The final broad candidate/frozen-baseline pairs cannot isolate this slice because
the candidate contains the earlier roadmap changes. The logged production pair
is also dispersed (-4.9% RPS with 5.9-point ratio MAD). The separate immediately
preceding-image A/B is the relevant regression gate for the shared `AccessLog`
refactor.

## Decision

Accept the internal format/batched-write boundary and retain bounded selector
access logging in the benchmark prototype. Use `drop`, 256 waiting records, an
eight-record batch, and a 1 ms collection window as the availability-oriented
one-CPU research baseline. Keep `wait` as the lossless alternative and always
report delivery percentage when comparing them.

Do not expose these controls or promote the selector backend yet. The logged
selector beats threaded production in this exact one-CPU cohort, but it does not
establish a universal win. It also adds a writer
thread, cross-thread ownership, queue policy, shutdown ordering, and failure
counters that a permanent backend would have to maintain.

Before promotion, test JSON and combined formats, slow/blocking filesystems,
rotation and reopen, full disks and permissions failures, multiple server CPUs,
free-threaded Python, log shipping through pipes, sustained overload that
actually saturates the 256-record budget, crash-loss bounds for each batch
window, and mixed static/listing/compression/digest fairness. Dynamic WSGI/ASGI
access logging remains a separate cohort.

## Verification

- 796 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds. The normal environment has 40 optional-dependency skips; the populated
  free-threaded environment has four.
- Repository-wide Ruff format/lint, Bandit, strict MkDocs, and `git diff --check`
  gates pass. Ty reports only two pre-existing unused-suppression warnings.
- The final benchmark artifact's product-tree and harness-file hashes match the
  current source.
- Wheel and source distribution build, the wheel has no unconditional runtime
  dependencies, and its installed import/CLI smoke passes outside the source
  tree.

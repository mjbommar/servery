# Static hot-path experiments — 2026-07-10

Status: one change accepted, one default change rejected, profiling follow-up
open. These are machine-specific research results, not portable reference numbers.

## Questions

1. How much of the small-static gap is body delivery versus request/path/thread
   overhead?
2. Is a bounded read/write faster than `sendfile` for small warm-cache files?
3. Can the existing reusable worker pool close the connection-churn gap without
   damaging keep-alive throughput or tail latency?

## Environment and controls

- External A/B runs used the comparison Docker image with CPython 3.15.0b3,
  normal GIL, one server CPU, four isolated client CPUs, plaintext HTTP/1.1,
  warm cache, and response status/length/SHA validation.
- Candidate strategies were rotated within the same image and trial schedule.
  This is stronger than comparing separate rebuilds, although three trials of
  three seconds still leave visible order/thermal noise.
- The client remained below saturation for small-file servery rows. All timed
  responses completed without errors.
- Machine-specific JSON is retained under ignored `benchmarks/artifacts/` paths
  named in the sections below.

## Directional profiles

### GIL-holding stack sample

`py-spy` did not yet support the installed CPython 3.15 alpha build, so the
directional stack profile used CPython 3.14 with the same source. It collected
979 GIL-holding samples during a one-core, 64-connection 1 KiB run. Sampling fell
behind the target rate, so percentages identify work areas rather than precise
cost accounting.

- About 26% of inclusive samples were in the main accept/dispatch path.
- Thread request startup/lifecycle was prominent; `threading.Thread.start` alone
  was about 7% of leaf samples.
- URL translation plus safe containment accounted for roughly 13% inclusive.
- Request parsing/header reading was distributed across many small Python frames.
- The `sendfile` wrapper was only about 3% inclusive; body delivery still matters
  through syscall/write behavior not represented by GIL-only samples.

### Syscall trace

A deliberately instrumented CPython 3.15 run served 4,069 requests under
`strace -f -c`. Tracing serialized/perturbed the threaded workload, so its
throughput is unusable. Counts showed the shape of one hot response:

- one receive, header send, and `sendfile` per request;
- one open and close, with stat/fstat validation;
- repeated path-component `lstat` calls from safe `realpath` containment;
- heavy futex activity under the perturbed threaded/GIL run.

The traced root was a deep development path and therefore performed more
component `lstat`s than the comparison container's `/srv` root. Path work remains
a candidate, but safe containment cannot be cached or bypassed without adversarial
invalidation evidence.

## Size sweep before the strategy experiment

The initial sendfile-first sweep showed that 0-byte responses were materially
faster than 1–16 KiB responses, while 1 KiB and 16 KiB throughput were nearly
identical. That implies a substantial fixed body-delivery cost, but also leaves
most total cost in parsing/path/header/lifecycle work.

| Body | Connections | Median throughput | p99 |
| ---: | ---: | ---: | ---: |
| 0 B | 1 | 18.0k req/s | 0.14 ms |
| 1 KiB | 1 | 14.8k req/s | 0.21 ms |
| 16 KiB | 1 | 14.8k req/s | 0.21 ms |
| 0 B | 64 | 21.0k req/s | 11.4 ms |
| 1 KiB | 64 | 14.8k req/s | 17.9 ms |
| 16 KiB | 64 | 14.9k req/s | 17.3 ms |

Artifact: `static-size-baseline-2026-07-10.json`.

## Buffered-read versus sendfile crossover

The benchmark ran sendfile and 1/4/16/64 KiB buffer thresholds as separate
Servery adapters inside the same image. Rows with thresholds below the body size
are classified as sendfile; thresholds at or above it are classified as buffered.
Equivalent threshold variants supply additional rotated samples of the same code
path.

| Body | Conns | Sendfile req/s | Buffered req/s | Change | Sendfile p99 | Buffered p99 | Peak-memory change |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 KiB | 1 | 13.06k | 15.74k | +20.5% | 0.23 ms | 0.16 ms | +0.2 MiB |
| 1 KiB | 64 | 15.01k | 18.03k | +20.1% | 17.59 ms | 14.47 ms | +0.6 MiB |
| 4 KiB | 1 | 13.37k | 15.54k | +16.3% | 0.22 ms | 0.15 ms | +0.8 MiB |
| 4 KiB | 64 | 15.38k | 18.56k | +20.6% | 17.55 ms | 13.67 ms | +1.6 MiB |
| 16 KiB | 1 | 13.34k | 15.07k | +12.9% | 0.21 ms | 0.16 ms | −0.1 MiB |
| 16 KiB | 64 | 16.09k | 18.34k | +14.0% | 16.03 ms | 13.22 ms | +2.5 MiB |
| 64 KiB | 1 | 14.50k | 15.37k | +6.0% | 0.21 ms | 0.15 ms | +1.6 MiB |
| 64 KiB | 64 | 14.52k | 14.04k | −3.3% | 17.65 ms | 17.36 ms | +7.4 MiB |

Artifacts: `static-buffer-crossover-2026-07-10.json` and
`static-buffer-crossover-large-2026-07-10.json`.

### Decision: accept 16 KiB, reject 64 KiB

Plain HTTP/1 now uses one bounded read/socket write through 16 KiB and retains
zero-copy sendfile above it. The policy is configurable as
`small_file_buffer_size` / `--small-file-buffer-size`; zero selects sendfile for
every nonempty file.

Why 16 KiB:

- it clears the roadmap's 10% end-to-end benefit gate at both tested
  concurrencies;
- p99 improves rather than trading throughput for queueing;
- aggregate transient bytes are bounded by the threshold and admitted
  connections;
- 64 KiB does not clear the concurrent-throughput gate and has a much larger
  observed memory cost;
- TLS and larger plaintext files retain their existing bounded-copy/sendfile
  paths.

Protected large-file and dynamic benchmarks, the full Linux conformance suite,
packaging/docs checks, installed-wheel import, and targeted free-threaded 3.14/3.15
tests have now passed. The repository CI matrix remains the authority for native
Windows and macOS behavior.

### Protected external validation

After promoting 16 KiB to the default, a fresh three-trial comparison reran small
static, large static, churn, WSGI, and ASGI cohorts. All responses were correct
and error-free.

| Protected workload | Earlier median | Accepted-default median | Direction |
| --- | ---: | ---: | ---: |
| 1 KiB static, 64 connections | 14.87k req/s, 18.55 ms p99 | 17.63k req/s, 13.44 ms p99 | +18.6% throughput, −27.5% p99 |
| 1 KiB churn, 32 connections | 5.54k req/s, 7.64 ms p99 | 6.11k req/s, 6.97 ms p99 | +10.3% throughput, −8.8% p99 |
| 8 MiB static, 16 connections | 2.73 GB/s | 3.35 GB/s | no regression; both client-limited |
| Trivial WSGI, 64 connections | 44.08k req/s | 46.01k req/s | no protected regression |
| Trivial ASGI, 64 connections | 33.92k req/s | 36.47k req/s | no protected regression |

The runs were separated in time, so the paired crossover—not these before/after
percentages—is the primary causal evidence. Competitor medians in the accepted
run were 33.15k req/s for Caddy and 91.39k for nginx at 1 KiB/64 connections;
the change narrowed servery's gap from about 2.1x to 1.9x versus Caddy and from
about 6.5x to 5.2x versus nginx on this host.

Artifact: `accepted-small-buffer-protected-2026-07-10.json`.

## Reusable worker-pool experiment

The existing `--max-workers` path was compared against thread-per-connection at
64 keep-alive connections and 32 churn connections.

| Workload | Default req/s | Pool req/s | Change | Default p99 | Pool p99 | Peak-memory change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns, 64 workers | 14.41k | 14.34k | −0.5% | 18.03 ms | 19.03 ms | +0.2 MiB |
| 1 KiB churn, 32 conns, 64 workers | 5.27k | 7.96k | +51.0% | 7.84 ms | 10.99 ms | −1.0 MiB |
| 1 KiB churn, 32 conns, 32 workers | 5.27k | 7.84k | +48.7% | 7.84 ms | 13.11 ms | −1.1 MiB |

Artifacts: `worker-pool-baseline-2026-07-10.json`,
`worker-pool-64-2026-07-10.json`, and `worker-pool-32-2026-07-10.json`.

### Decision: retain as explicit policy; do not make it the default

Thread reuse materially increases churn throughput and slightly reduces memory,
but the executor queue worsens churn p99 by 40–67% and does not improve keep-alive
throughput. This fails the roadmap's tail-latency/default gate. `--max-workers`
remains useful when an operator prefers bounded/reused workers, but the default
does not change.

The result strengthens the case for a selector/static frontend spike: avoiding
per-connection threads may address both churn throughput and tail latency without
queueing whole keep-alive connections behind a worker pool.

## Path and MIME microbenchmarks

These directly callable CPython 3.15 measurements are diagnostic only. They do
not clear the end-to-end acceptance gate.

- Replacing the stdlib translation plus containment check with the existing
  `safe_join` helper reduced path-resolution time from 5.81 to 4.98 microseconds
  per path (about 14%). That is less than one microsecond per request and changes
  semantics: `/a/../b` resolves to `/b` today but `safe_join` deliberately drops
  `..` and produces `/a/b`; trailing-slash and malformed-surrogate behavior also
  differ. Duplicating the stdlib translator for an estimated low-single-digit
  end-to-end gain is rejected pending stronger evidence.
- A bounded cache around MIME fallback reduced a five-extension microbenchmark
  from about 0.82 to 0.29 microseconds per lookup. The expected end-to-end gain is
  below the provisional 5% noise floor, while caching interacts with mutable
  `mimetypes`/`extensions_map` state and adds a lock under free-threaded Python.
  It is deferred rather than promoted.
- Caching containment results is not acceptable without symlink-swap,
  replacement, permission-change, case-folding, and Windows drive/UNC evidence.
  A stale positive result would lengthen the existing lookup/open race and can
  become a security bug, not merely stale metadata.

The larger opportunity is anchored lookup: on Linux, `openat2` could combine
containment and open without repeatedly walking the absolute root, but CPython
3.15 does not expose `openat2`. A `ctypes` syscall would add platform/ABI and
security-review surface inconsistent with a low-risk optimization. Keep it as a
backend-spike question, not a production shortcut.

### Decision: preserve path semantics; defer MIME caching

The measured savings do not meet the Phase 2 acceptance threshold. The benchmark
harness now reports min/max, MAD, and an explicit client-limited marker so future
small claims cannot hide behind a median. It also hashes the complete Python
product tree in every artifact, closing a provenance hole found during this pass.

## Next work

1. Let the native CI matrix confirm Windows and macOS fallback behavior.
2. Use the measured selector ceiling to design shared request/response planning;
   do not promote the benchmark spike without full wire/conformance parity.
3. Add high-cardinality and cold-cache path cohorts before reconsidering safe
   metadata caches or OS-specific anchored lookup.

# Connection backend decision — 2026-07-11

Status: accepted for `EDGE-020`. Keep the threaded HTTP/1 backend as the only
production backend. Keep the selector implementation benchmark-only, under its
explicit comparison-harness server name, until the promotion gates below pass.

## Decision

Servery does not add a public `connection_backend` setting yet. The shipped
threaded backend remains the portable production choice for static, WSGI, ASGI,
TLS, WebSocket, and proxy workloads. The selector prototype remains isolated in
`benchmarks/comparison/selector_prototype.py`; it is not imported by the runtime,
listed in the CLI, or selected implicitly by a profile.

This is a deliberate compatibility decision rather than a rejection of the
selector architecture. The prototype demonstrates that one event loop can
reduce connection scheduling overhead, especially for churn and tail latency.
It does not yet demonstrate that servery can safely give every supported
workload to that loop, nor that a split product with two independently evolving
HTTP servers would be maintainable.

If the missing gates pass, the eventual configuration shape may be
`connection_backend = "threaded" | "selector"`, with `threaded` retained as a
portable fallback. That setting should be introduced only when `selector` names
a supported product capability, not while it names a benchmark hypothesis.

## Evidence considered

The production-shaped plaintext static prototype includes incremental parsing,
pipelining state, bounded admission, head/idle/write deadlines, request-count
limits, backpressure, lifecycle counters, and bounded drain. It reuses shared
request, range, conditional, directory, download, SPA, compression, digest, and
listing policy where those seams exist.

On the controlled one-CPU CPython 3.15.0b3 comparison:

| Workload | Selector result versus threaded | Interpretation |
| --- | ---: | --- |
| 1 KiB, 64 keep-alive | +7.4% RPS, -70.8% p99 | Strong tail result; throughput gain is too small to justify a second backend alone. |
| 1 KiB, 32 connection churn | +62.0% RPS, -41.7% p99 | Material architecture signal, but selector RPS MAD was 16.4%. |
| 16 KiB, 64 keep-alive | -1.3% RPS, -73.5% p99 | Near throughput parity with substantially better tail latency. |
| 64 KiB, 64, 16 KiB buffer threshold | -21.4% RPS, -65.3% p99 | The transport/body crossover is backend-specific. |
| 64 KiB, 64, 64 KiB buffer threshold | +2.1% RPS, -69.5% p99 | A larger read can recover throughput, with a larger per-connection memory budget. |
| 64 KiB, 256, 64 KiB buffer threshold | +18.5% RPS, -76.2% p99 | Promising under concurrency, but not evidence for slow readers, TLS, or broad platforms. |

These are directional same-host results, not default-selection proof. The
historical harness pinned the server to logical CPU 0 and clients to CPUs 1–4;
on the measured i7-12700K, CPUs 0 and 1 are SMT siblings on one physical core.
The sets are logically disjoint, and rotated comparisons remain useful, but the
older records overstate physical isolation. The churn result also exceeds a
reasonable dispersion gate. A physically isolated rerun was attempted, but the
current harness's framework-readiness probe was incompatible with the frozen
pre-probe selector image, so no replacement measurement was produced.

The read-versus-streaming choice therefore remains policy, not dogma. Bounded
buffering is effective for small plaintext files; streaming or `sendfile` keeps
large-response memory bounded; TLS needs its own chunking crossover because it
cannot use the same kernel path. The current `small_file_buffer_size` control is
the right experimental seam. A selector-specific default may eventually be
appropriate, but only after retained-memory and slow-consumer gates.

Blocking work is the other decisive tradeoff. Moving every warm filesystem
lookup to a bounded executor reduced 1 KiB keep-alive throughput by 29.8% and
increased p99 by 70.7%, erasing the selector's warm-cache advantage. Inline
metadata acquisition is fast but can stall the whole loop on cold, remote, or
unreliable storage. The production design must classify, bound, and expose
blocking-work policies instead of choosing either “always inline” or “always in
a pool.” That work belongs to `EDGE-021`.

## Capability assessment

| Area | Current evidence | Decision consequence |
| --- | --- | --- |
| HTTP/1 parsing and pipelining | Shared validation plus prototype state and adversarial tests | No `100 Continue`; bodies force close; HTTP/0.9 and method/feature dispatch differ. Differential wire tests remain. |
| Admission, deadlines, cancellation, drain | Explicit prototype limits and focused failure tests | Shape accepted for further development. |
| Static GET/HEAD | Broad benchmark-only coverage with shared policy | Not enough to represent the whole product. |
| Access logging | Bounded batched prototype; logged RPS advantage only 8.1% in one cohort | Needs format, sink-failure, rotation, and saturation gates. |
| Filesystem/listings/compression/digests | Bounded specialist pools exist in the prototype | Consolidate into a production scheduler; do not ship independent knobs yet. |
| Request bodies and uploads | A body forces connection close after one response and is not consumed | No parity. |
| TLS | Unsupported by the prototype | Production promotion is prohibited. |
| WSGI and ASGI | Unsupported by the prototype | Production promotion is prohibited. |
| WebSockets and proxying | Unsupported by the prototype | Production promotion is prohibited. |
| HTTP/2 and HTTP/3 | Outside the prototype | Keep separate backend/transport decisions. |
| Platforms | Performance evidence is Linux-specific; no macOS/Windows lifecycle gate | Threaded remains the portable fallback. |

The focused selector, static-contract, request-parser, and HTTP/1 conformance
suites pass 88 tests, but this is not a differential selector-versus-threaded
wire corpus. Other correctness gaps include allowing another keep-alive request
during graceful drain, an unbounded default response-write wait when
`write_timeout` is unset, silent feature divergence for archive/selection query
paths, and different error/security headers. Those are release blockers, not
documentation polish.

## Promotion gates

The selector can become a public experimental backend only after all of these
are true:

1. `EDGE-021` provides shared bounded blocking-work scheduling with separate
   cheap and expensive budgets, observable saturation, cancellation ownership,
   and continued cheap-request progress.
2. `EDGE-022` passes differential HTTP/1 wire tests for static responses,
   request bodies, disconnects, pipelining, slow readers, timeouts, and drain,
   while reusing parsers and response planning instead of forking policy.
3. TLS handshake, read, write, shutdown, slow-client, and certificate-reload
   tests pass; TLS-specific buffering/streaming thresholds have memory and
   throughput evidence.
4. WSGI, ASGI HTTP and streaming, WebSockets, and proxy feature routing is either
   implemented with parity or rejected at configuration time without fallback
   surprises.
5. Linux production benchmarks show a material, repeatable improvement in at
   least the intended high-concurrency/churn cohort without protected static,
   streaming, TLS, or dynamic regressions. Trials must include dispersion,
   errors, CPU, memory, queue occupancy, and slow-consumer behavior.
6. macOS and Windows retain a tested threaded fallback. A production selector
   claim on either platform requires its own event-loop and lifecycle evidence.
7. The selector code moves behind a shared runtime boundary with maintainable
   ownership; the benchmark adapter itself is never relabeled production.

For a production-default proposal, use at least seven rotated trials and require
either at least 20% median RPS or 40% p99 improvement in the intended
high-concurrency/churn cohort, at least 50% lower incremental PSS at 10,000 idle
connections, zero correctness errors, and no protected cohort regression larger
than 5% RPS, 10% p99, or 10% memory. Ratio MAD should be at most 5% or a suitable
confidence interval should exclude neutrality. Multiworker scaling should retain
at least 80% efficiency at two and four cores without accept imbalance. These
thresholds are decision gates, not promises that every workload must get faster.

Promotion from public experimental to a production default requires release
gates across the first production scope, not only static benchmarks. If the
selector wins only for plaintext static traffic, servery should route that
cohort explicitly or retain the threaded default rather than silently changing
semantics for dynamic applications.

## Consequences and next work

- `EDGE-020` is closed by selecting the threaded production default and an
  explicit benchmark-only selector candidate.
- `EDGE-021` is next because blocking-work isolation is required by either
  connection model and directly addresses the free-threaded/high-concurrency
  overload already observed.
- `EDGE-022` owns protocol and application parity. It must not be hidden inside
  performance work.
- No public configuration or compatibility burden is added by this decision.
- Future benchmark records should compare the candidate against the current
  threaded runtime, not the historical pre-optimization baseline.

## Source records

- [Selector frontend spike](2026-07-10-selector-spike.md)
- [Production-shaped selector prototype](2026-07-10-selector-production-prototype.md)
- [Selector filesystem offload](2026-07-10-selector-filesystem-offload.md)
- [Shared conditional and range selection](2026-07-10-shared-range-selection.md)
- [Shared directory redirect and index selection](2026-07-10-shared-directory-index.md)
- [Shared download disposition and SPA fallback](2026-07-10-shared-download-spa.md)
- [Selector compression and transient single-flight](2026-07-10-selector-compression.md)
- [Opened-identity digests and bounded selector hashing](2026-07-10-selector-digests.md)
- [Shared listing policy and bounded selector rendering](2026-07-10-selector-listings.md)
- [Bounded selector access logging](2026-07-11-selector-access-logging.md)

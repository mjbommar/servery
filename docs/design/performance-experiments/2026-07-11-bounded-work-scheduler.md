# Bounded blocking-work scheduler — 2026-07-11

Status: `EDGE-021` in progress. The shared admission/ownership primitive and
physically isolated work lanes and declared retained-byte admission are
implemented and unit-tested. Production work classification, accurate per-work
byte charging, transport integration, mixed-load benchmarks, and default policy
remain open.

## Why a separate scheduler is necessary

The threaded HTTP/1 server currently has two connection policies:

- without `max_workers`, each admitted connection gets a thread;
- with `max_workers = N`, a connection executor runs `N` handlers and admits up
  to `4N` active-plus-queued handler submissions.

That executor bounds whole connection handlers, not individual blocking jobs.
It must not also execute nested listing, compression, digest, or application
work: if all connection workers submit nested jobs and wait, the same saturated
executor cannot run those jobs.

The async situation is different but not safer. HTTP/3 currently uses
`asyncio.to_thread`, whose default executor has no servery-owned queue budget.
The benchmark selector has four carefully bounded specialist executors, but
they are duplicated research code rather than a backend-neutral runtime
contract.

## Current blocking-work inventory

| Work | Current execution and bound | Main gap |
| --- | --- | --- |
| Static lookup/open/small read | Inline in a connection thread; connection admission is bounded | Blanket async offload costs about 30% warm-file RPS, but cold/remote storage can stall an event loop. |
| Large plaintext response | `sendfile` or bounded userspace chunks | Preserve streaming; never queue a full large body. |
| TLS response | Bounded userspace chunks | Needs a TLS-specific crossover study, not the plaintext buffer threshold by assumption. |
| Compression | Whole eligible file read and encode in the connection worker, with cache single-flight | Distinct misses can occupy all workers and multiply buffered memory. |
| Representation digest | Whole opened identity hashed in the connection worker, with same-key sharing | Distinct large identities can starve cheap files. |
| Directory listing | Scan, metadata, sort, render, and optional compression inline | Entry/page limits bound size but not concurrent CPU/I/O work. |
| Archive | Walk/compress and socket writes inline | Body memory is streamed, but slow consumers pin CPU/workers. |
| Upload/extraction | Body/disk streaming and optional extraction inline | Receive and extraction need different policies; running extraction is not thread-cancellable. |
| WSGI/CGI/proxy | Application, child process, or upstream call in connection worker | Only the whole-connection limit applies. |
| Access log | Synchronous format/write/flush under the log lock | Slow log storage serializes response completion. |
| HTTP/2 | Static preparation occurs in its connection thread | One expensive stream can block unrelated streams on that connection. |
| HTTP/3 | Response preparation and every file chunk use the default executor | Queue ownership and shutdown are not explicit. |

## Accepted architecture

`src/servery/_work.py` now provides:

- `BoundedWorkLimiter` and idempotent leases for threaded handlers that should
  execute inline under a job/byte budget instead of submitting nested work;
- `BoundedWorkPool`, which places an exact `workers + queue_capacity` bound
  around `ThreadPoolExecutor` and rejects nonblockingly before invoking work;
- capacity ownership until a call actually completes, even if its requester is
  cancelled;
- an async adapter with an explicit late-result disposer for resources such as
  opened files;
- bounded-cardinality active, queued, high-water, submit, reject, success,
  failure, cancellation, retained-byte, and rejection-reason snapshots;
- optional byte-weighted admission held until actual completion, including for a
  cancelled requester;
- idempotent admission close plus queued-future cancellation;
- `WorkScheduler`, which gives filesystem, compute, stream, and application
  classes physically separate executors.

Physical separation is intentional. A priority or semaphore in front of one
`ThreadPoolExecutor` does not stop already-queued expensive jobs from sitting in
its internal FIFO ahead of cheap work. Separate lanes make the progress
guarantee direct and testable.

The inline limiter is equally important: it lets a configured number of
connection workers perform expensive work while reserving other connection
workers for cheap requests. It avoids the deadlock and context-switch cost of
submitting nested work merely to wait synchronously for it.

The focused tests prove exact capacity and recovery, no invocation on rejection,
queued cancellation, running-cancellation ownership and late disposal,
submit/worker failure cleanup, close behavior, and cheap filesystem progress
while the compute lane is full. A separate server regression test covers exact
slot and connection cleanup if the existing connection executor rejects a
handoff.

## Proposed work-class policy

| Lane | Intended work | Important constraint |
| --- | --- | --- |
| Filesystem | containment/index lookup, open/stat, bounded small read | Inline remains the threaded/warm default; async backends require a bounded policy. |
| Compute | compression, digests, listing render, post-upload extraction | Add a retained-input/output byte budget, not only a job count. |
| Stream | archive generation and other long producers | Use a byte-bounded producer/transport channel; slow clients must backpressure and cancel. |
| Application | future selector WSGI and explicitly blocking user/upstream work | Keep separate from internal work; arbitrary running Python cannot be killed safely. |

The eventual configuration should keep these budgets per worker and expose
aggregate derived values for `workers > 1`. It must not repurpose
`max_workers`, whose present meaning is connection-handler workers. Start with
a small surface—lane workers, queue entries, and compute/stream bytes—and add
subtype controls only if measurement proves they are operationally necessary.

## Admission and cancellation contract

Admission is atomic and nonblocking. Saturation or drain must be known before
expensive work and, where HTTP semantics require an error, before response
headers. Optional work may take a semantic fallback: for example, identity can
replace compression only when content negotiation accepts identity. Required
digests, archive generation, or application execution cannot be silently
omitted.

Queued cancellation removes the job and releases capacity. Python cannot safely
interrupt an arbitrary running thread, so running cancellation detaches the
requester while the job retains its permit. A registered disposer owns any
successful late result. The process supervisor remains the hard deadline for a
stuck syscall or application call.

## Read, buffer, and streaming choices

The scheduler does not replace transport policy:

- keep `small_file_buffer_size` as a configurable bounded plaintext fast path;
- keep large plaintext on `sendfile` where supported;
- use bounded userspace chunks for plaintext fallback and TLS;
- queue handles, immutable descriptors, or bounded chunks, never an unbounded
  response body;
- charge compression input and output retention to a byte budget;
- hash from the opened identity in bounded chunks;
- receive uploads before admitting optional extraction, so a slow sender does
  not occupy a compute worker.

This preserves the measured tradeoff: always pooling warm filesystem work
reduced 1 KiB keep-alive throughput by 29.8% and increased p99 by 70.7%, while
always doing filesystem work on an event loop makes one cold NFS/page-cache
stall global. The storage policy should be explicit and configurable.

## Verification plan

Before closing `EDGE-021`:

1. Add subtype fairness inside compute and drain-deadline reporting; apply the
   existing byte-weight admission to accurate input/output estimates at each
   integration point.
2. Integrate production access logging, async filesystem preparation,
   listing/compression/digest, archive streaming, post-upload extraction, and
   HTTP/3 without default-executor calls. Preserve WSGI/CGI/proxy ownership until
   their cancellation semantics are explicit.
3. Add differential integration tests for rejection before headers, semantic
   fallbacks, late file/temp cleanup, worker shutdown, free-threaded races, and
   multiworker aggregate accounting.
4. Extend the load generator with simultaneous cheap and expensive cohorts.
   Report each cohort independently rather than hiding starvation in aggregate
   RPS.
5. Run at least seven rotated trials. Protected small/large baselines should
   remain within 5% RPS, 10% p99, and 10% memory with ratio MAD at most 5%.
   During expensive saturation, cheap requests must remain all-200, complete in
   every one-second interval, retain at least 80% of cheap-alone RPS, and keep
   p99 within the larger of 2x baseline or baseline plus 10 ms.
6. Verify outstanding expensive work never exceeds workers plus queue, rejection
   is observed, recovery succeeds, and a 60-second saturation run reaches a
   steady memory plateau.

These thresholds are engineering acceptance budgets, not portable public
performance claims.

The first production integration is now recorded in
[Production bounded access-log handoff](2026-07-11-production-access-log-scheduler.md).
It adds count- and byte-bounded batched file logging with explicit block/drop
policy, but controlled results retain synchronous logging as the default because
no lossless async shape passed the p99 gate. Broader transport commit hooks and
slow-sink mixed tests remain open.

HTTP/1 archive and checkbox-selection bodies now have the first inline stream
lease integration. `max_archive_streams` is an optional per-worker limit acquired
before `200`/chunked headers and held through the final chunk or abort. Saturation
returns `503` with `Retry-After: 1`; `HEAD` performs no body-work admission.
Configuration rejects a stream limit that would consume every finite
`max_workers` handler. Focused integration tests prove saturation, cheap static
progress, `HEAD`, and recovery. The default remains unlimited until mixed archive
evidence selects a production-profile value.

The first part of item 4 is implemented: `scripts/loadgen.py` now has a
synchronized `run_mixed_load()` API with independently reported cohorts and
per-second completion counts. The comparison runner still needs scenario/config
plumbing, scheduler telemetry capture, and decision-grade runs.

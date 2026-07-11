# Performance and production-gap research roadmap

Status: in progress as of 2026-07-11. This roadmap turns the external comparison results
into a sequence of research, design, prototype, and implementation decisions. It
does not assume that every gap should be closed or that nginx/Caddy architecture
should be copied into a zero-dependency Python file server.

Execution has begun. The first static hot-path profiles, size-aware buffering
decision, and worker-pool decision are recorded in
[Static hot-path experiments — 2026-07-10](performance-experiments/2026-07-10-static-hot-path.md).
The first architecture ceiling test and its write-batching control are recorded
in [Selector frontend spike — 2026-07-10](performance-experiments/2026-07-10-selector-spike.md).
The follow-on boundary and incremental migration are specified in
[Static response planning boundary](static-response-planning.md).
The first accepted shared-planning slice and paired performance decision are in
[Shared static representation plan — 2026-07-10](performance-experiments/2026-07-10-shared-static-plan.md).
The accepted request-line/header convergence slice, including the initially noisy
tail-latency result and longer paired resolution, is in
[Shared HTTP/1 request parser — 2026-07-10](performance-experiments/2026-07-10-shared-request-parser.md).
The first configurable connection-lifecycle slice and the rejected ASGI parser
convergence attempt are in
[HTTP/1 request-count connection policy — 2026-07-10](performance-experiments/2026-07-10-connection-policy.md).
The next connection-occupancy slice is in
[HTTP/1 keep-alive idle timeout — 2026-07-10](performance-experiments/2026-07-10-keepalive-timeout.md).
The first realistic dynamic-response slice is in
[ASGI streaming response backpressure — 2026-07-10](performance-experiments/2026-07-10-asgi-stream-backpressure.md).
The cross-transport stalled-writer policy and its rejected hot-path shapes are in
[Response write-progress timeout — 2026-07-10](performance-experiments/2026-07-10-write-progress-timeout.md).
The total HTTP/1 body-consumption budget and new request-body comparison cohorts
are in
[Total HTTP/1 request-body timeout — 2026-07-11](performance-experiments/2026-07-11-request-body-timeout.md).
The first production-shaped selector gate is in
[Production-shaped selector prototype — 2026-07-10](performance-experiments/2026-07-10-selector-production-prototype.md).
The blocking-filesystem strategy decision is in
[Selector filesystem offload — 2026-07-10](performance-experiments/2026-07-10-selector-filesystem-offload.md).
The first shared range/conditional selection and selector semantic gate is in
[Shared conditional and range selection — 2026-07-10](performance-experiments/2026-07-10-shared-range-selection.md).
Directory redirect/index convergence is in
[Shared directory redirect and index selection — 2026-07-10](performance-experiments/2026-07-10-shared-directory-index.md).
Download disposition and SPA convergence, including the fallback containment
fix, is in
[Shared download disposition and SPA fallback — 2026-07-10](performance-experiments/2026-07-10-shared-download-spa.md).
Selector compression and the accepted production single-flight improvement are
in [Selector compression and transient single-flight — 2026-07-10](performance-experiments/2026-07-10-selector-compression.md).
Opened-identity digest correctness, transient production sharing, and bounded
selector hash scheduling are in
[Opened-identity digests and bounded selector hashing — 2026-07-10](performance-experiments/2026-07-10-selector-digests.md).
Shared listing request policy and bounded selector scan/render scheduling are in
[Shared listing policy and bounded selector rendering — 2026-07-10](performance-experiments/2026-07-10-selector-listings.md).
Bounded selector access-log handoff, overload policy, batching, and delivery
auditing are in
[Bounded selector access logging — 2026-07-11](performance-experiments/2026-07-11-selector-access-logging.md).
Lazy real-peer ASGI disconnect delivery and its rejected eager-event shapes are
in [Lazy ASGI peer-disconnect delivery — 2026-07-11](performance-experiments/2026-07-11-asgi-peer-disconnect.md).
Lazy request-scope notification after response completion is in
[ASGI post-response disconnect — 2026-07-11](performance-experiments/2026-07-11-asgi-post-response-disconnect.md).
The advertised ASGI HTTP 2.4 send-on-closed exception boundary is in
[ASGI 2.4 closed-send errors — 2026-07-11](performance-experiments/2026-07-11-asgi-closed-send.md).
Strict response event ordering, exact framing, and negotiated HTTP trailers are
in [ASGI response ordering, framing, and trailers — 2026-07-11](performance-experiments/2026-07-11-asgi-response-state.md).
Pinned Starlette/FastAPI compatibility, external paired cohorts, and the current
CPython 3.15 Pydantic-wheel boundary are in
[ASGI framework compatibility and comparison — 2026-07-11](performance-experiments/2026-07-11-asgi-framework-compatibility.md).
Explicit portable/native Uvicorn adapters, bounded latency evidence, and the
100/1,000/10,000 connection-scaling decision are in
[Native Uvicorn and ASGI concurrency scaling — 2026-07-11](performance-experiments/2026-07-11-asgi-native-scaling.md).
Spec-correct startup/shutdown failure, configurable lifecycle policy, and
shallow-copied HTTP/WebSocket state are in
[ASGI lifespan policy, failure, and state — 2026-07-11](performance-experiments/2026-07-11-asgi-lifespan-policy.md).
Strict non-`Host` ASGI field syntax, header-count scaling, and the rejected
validation shapes are in
[Strict ASGI HTTP/1 field syntax — 2026-07-11](performance-experiments/2026-07-11-asgi-field-syntax.md).
The production total request-head budget, phase-correct keep-alive boundary, and
configured-path cost are in
[Total HTTP/1 request-head timeout — 2026-07-11](performance-experiments/2026-07-11-request-head-timeout.md).

Current execution checkpoint: the size-aware 16 KiB plaintext path is accepted;
the reusable thread pool is rejected as a default; unsafe path caching and a MIME
cache are deferred; response write coalescing is rejected; the selector ceiling
signal is confirmed but not production-ready; and shared opened-file plus HTTP/1
request-head parsing primitives are accepted. A configurable HTTP/1 requests-per-
connection limit is accepted for threaded/WSGI/ASGI, disabled generally and set
to 1,000 by the `cdn`/`app` profiles. A distinct opt-in keep-alive idle timeout
is also accepted without changing profile defaults. Dynamic realism,
multi-process scaling, later pipeline/application phase deadlines, large
header/cookie and proxy-differential corpora, bandwidth/fairness policy,
production selector failure modes, and protocol/deployment tiers remain open.
The first dynamic realism gate is closed: intermediate ASGI event drains reduced
four-client slow-reader cgroup peak memory from 283.5 MiB to 40.4 MiB (85.7%)
without a material throughput or tail-latency change, and blocked-drain task
cancellation is covered directly.
An independent write-progress timeout is now accepted across synchronous HTTP/1,
WSGI/proxy, HTTP/2, ASGI HTTP/WebSocket, and HTTP/3. It is opt-in because timeout
values are workload-specific; the disabled path is performance-neutral, while a
same-image enabled ASGI control measured +2.1% RPS / +1.6% p99 with dispersion
wider than both effects. A separately configurable total HTTP/1 request-body
budget is also accepted. It starts lazily on first consumption, spans progress
and application pauses, remains disabled by default, and covers upload, WSGI,
CGI, proxy, WebDAV, and ASGI declared/chunked bodies. Its disabled static/WSGI/
ASGI paths are neutral; enabled 64 KiB body throughput is -4.38% for WSGI and
neutral/noisy for ASGI.
An opt-in total HTTP/1 request-head budget is now also accepted. It begins after
the first byte ends the keep-alive idle phase, spans the request line and fields
without resetting on progress, preserves pipelined/body bytes, and remains off
in every profile. The final disabled static/WSGI source gates are neutral; the
enabled policy has a real tiny-dynamic capacity tradeoff at 64 connections
(-13.9% WSGI and -8.2% ASGI RPS), resolving smaller/noisier at concurrency one.
That measured cost is why this abuse-control choice is configurable rather than
unconditional. Header/cookie/TLS corpora, later pipeline/application phases,
and bandwidth policy remain open.
ASGI response completion now wakes terminal `receive()` calls with request-scope
`http.disconnect` without closing or consuming from the reusable HTTP/1 stream.
The ordinary keep-alive gate is neutral (-2.3% RPS/-0.4% p99 with wider RPS
dispersion); churn direction is unresolved across contradictory, high-dispersion
runs while p99 and memory remain neutral. Subscriber futures/sets exist only for
applications that actually wait. ASGI sends after response completion, to an
already-closing writer, or through native peer write failure now raise a server-
specific `OSError`; uncaught lifecycle errors remain quiet and preserve a fully
framed pipeline. The protected keep-alive/churn gate is neutral (-3.4%/-2.9%
RPS and +1.3%/+0.7% p99). Native competitor acceleration is now measured
separately rather than hidden behind Uvicorn auto-detection. The basic native
Starlette/FastAPI corpus is also closed; broader middleware/lifecycle behavior
remains open.
ASGI response start/body/trailer ordering and exact `Content-Length` are now
strict. The HTTP trailers extension is advertised on every HTTP scope, while
HTTP/1 trailer fields reach the wire only for `TE: trailers`; unnegotiated
events are validated and consumed so application lifecycle does not change.
Routing/framing trailers are rejected, and these safety semantics are not
configurable. The final 16-event streaming point is neutral (-1.0% RPS with
13.1-point dispersion); minimal 64-connection throughput remains an unresolved
exception at -13.7%, contradicting a +2.2% probe and a noisy -7.2%
concurrency-one control. High-event/header scaling and a
dedicated-host minimal rerun remain open.
The selector candidate now owns pipelined bytes, admission, total head and write
deadlines, request counts, counters, and graceful task drain in a benchmark-only
prototype. It retains +62.0% churn throughput and much lower p99, but only +7.4%
1 KiB keep-alive throughput and incomplete static semantics. A configurable
64 KiB bounded-buffer control fixes the asyncio-sendfile crossover without a
measured memory increase. No public/default backend is accepted.
Warm filesystem offload is rejected as the selector default after a measured
29.8% keep-alive regression. A cancellation-safe bounded pool remains an
explicit slow-storage research mode: under an injected 10 ms lookup it scales
from 98 inline RPS to 394 with four workers and 1,576 with sixteen, with bounded
memory and immediate `503` saturation behavior.
Single-range, `If-Range`, and conditional `304` decisions now share one opened-
identity primitive between production HTTP/1 and the prototype. The optimized
production adapter stays inside the protected budget (-4.3% range and -4.2%
`304` paired RPS). Selector range throughput reaches parity only with the
explicit 64 KiB buffer control, while p99 remains 74.7% lower; bodyless `304`
throughput remains 5–10% below production despite about 70% lower p99. These are
semantic-convergence and architecture results, not grounds for a default change.
Directory redirect and contained index selection now also share narrow helpers.
The production refactor is neutral (+1.2% paired RPS); the selector's indexed
file throughput is too dispersed for a claim, while p99 remains 72.6% lower.
Listings deliberately remain `501` in the prototype until their scan, metadata,
pagination, and generated-page policies can be preserved without event-loop
blocking.
Download query/disposition semantics and opt-in SPA fallback now also converge.
The production query refactor is neutral on the protected no-query path (-0.5%
keep-alive / -0.8% churn paired RPS), and SPA index lookup now revalidates
containment, closing an escaping-symlink gap. Selector SPA has no defensible RPS
direction but retains 71.1% lower p99.
Compression semantics and miss ownership are now closed for the benchmark
prototype. Transient same-key sharing with retained cache disabled improves
production uncached gzip by 91.2% paired RPS and 24.8% p99; warm-cache behavior
is neutral. The bounded selector remains 65.3% faster with 78.7% lower p99 on
that uncached same-file workload. Multi-key/multi-CPU scaling, TLS/platform
fallback and broader feature routing remain open.
Representation-digest semantics and miss ownership are now also closed for the
prototype. Production hashes the already-opened identity, rejects truncation
before file headers, and shares concurrent same-identity work without retaining
entries. That change measured +15.9% paired RPS with neutral p99. The selector's
separate bounded digest workers reached +29.2% RPS and -77.0% p99 versus improved
production. A shipped retained digest cache is deferred: entry memory is easy to
bound, but metadata-based invalidation and sequential mutable-file behavior need
broader evidence. HTTP/2/3 digest parity, large/high-cardinality files,
multi-CPU scaling, and slow/cold storage remain open.
Generated listing semantics and scheduling are now also closed for the benchmark
prototype. Query/theme policy and the generated-page CSP are shared; scan, stat,
render, and optional compression work run in a dedicated bounded executor with
immediate `503` saturation. The production refactor is neutral at 100 and 1,000
entries. On one server CPU, one selector listing worker matches or improves RPS,
cuts p99 65.0–71.1%, and reduces peak memory 49.5–61.9 MiB versus production.
Four workers help only the smaller page and cost more memory/tail latency, so one
worker is the conservative research baseline. Larger directories, mixed-work
fairness, slow storage, multi-CPU/free-threaded scaling, and platform tiers remain
open.
Access-log event-loop safety and overload ownership are now closed for the
benchmark prototype. One writer thread accepts immutable records through a
bounded budget with explicit `drop` or async `wait` saturation policy, drains
accepted records on shutdown, and counts omissions and failures. An eight-record
batch plus a configurable 1 ms collection window reduces per-line flush cost
without delaying response tasks beyond queue admission. In the final one-CPU
logged cohort, selector `drop` is 8.1% above current production RPS with 3.1%
lower p99 and 1.2 MiB lower peak memory; `wait` is 7.3% above with 9.7% lower
p99, although its RPS dispersion is much wider.
Every logged adapter delivered 100% of timed records, so drop received no hidden
omission advantage. Slow destinations, rotation/reopen, full disks, sustained
queue saturation, multiple CPUs, free-threading, and dynamic access-log cohorts
remain open. The controls remain benchmark-only.

The request-parser audit also closed the pre-existing `FR-HOST-01` standards gap:
missing, duplicate, or invalid HTTP/1.1 `Host` fields now fail with `400` and
connection close in threaded, selector, and ASGI serving. The residual ASGI
non-`Host` syntax gap is now closed without replacing its specialized byte
parser: small heads match non-`Host` lines individually, while heads above eight
fields use one possessive compiled block scan. Missing colons, invalid names,
whitespace before colons, forbidden value controls, and obsolete folding fail
with `400`; the valid 100-field ceiling remains `431`. Three shared-parser
adapters and the earlier unconditional validator remain rejected after 14–28%
throughput regressions.
The accepted field validator measures -3.35% RPS/+3.66% p99 on 64-connection
minimal ASGI and -5.01%/+3.60% on a new 32-field cohort. A concurrency-one
resolution measures -4.17% header-heavy RPS; churn remains too dispersed for a
directional claim, with a -0.65% point estimate. Per-line-only (-15.5% RPS),
ordinary block-regex (-6.7%), and valid-name-cache (-8.1%) large-head shapes were
rejected. The earlier byte-native Host check costs 0.7% throughput / +4.0% p99
at 64 connections and 2.2% / +1.6% in its longer concurrency-one gate.
This is a non-configurable desynchronization boundary; only resource policies,
not acceptance of malformed wire syntax, belong in operator configuration. The
optimized checks remain within the protected budget (−3.4% keep-alive and −2.6%
churn throughput); the initial Python per-byte implementation was rejected after
a measured 12.5% keep-alive regression.

## Desired outcome

Improve servery as far as the evidence supports while preserving the properties
that make it useful:

- pure Python and zero third-party runtime dependencies in the core;
- safe path containment and protocol correctness;
- simple one-process operation for ad-hoc and LAN use;
- portable behavior across Linux, macOS, and Windows;
- explicit, bounded resource policy instead of hidden unbounded work;
- competitive large-file delivery and strong minimal WSGI/ASGI paths.

The work has two different success conditions:

1. **Performance:** materially close the small-static and connection-churn gaps
   without regressing large transfers, dynamic handlers, correctness, or memory.
2. **Production capability:** make servery a credible, directly exposed
   single-service production edge with no required external proxy or process
   manager. The bounded scope and agent-sized work are defined in the
   [production-edge execution backlog](production-edge-execution-backlog.md).

## Evidence that motivates the roadmap

The 2026-07-10 standard comparison used CPython 3.15.0b3 with the GIL enabled,
one server CPU, four isolated client CPUs, one application worker, warm cache,
plaintext HTTP/1.1, three five-second trials, and deterministic server-order
rotation. All 102 timed samples completed with correct payloads and zero errors.

| Scenario | servery | Comparison | What it says |
| --- | ---: | ---: | --- |
| 1 KiB static, 64 connections | 14.9k req/s, 18.6 ms p99 | Caddy 31.1k / 6.5 ms; nginx 96.0k / 1.1 ms | The main CPU/tail-latency gap |
| 1 KiB connection churn, 32 connections | 5.54k req/s, 7.6 ms p99 | Caddy 15.3k / 3.7 ms; nginx 39.6k / 1.3 ms | Thread/handler/accept lifecycle is expensive |
| 8 MiB static, 16 connections | 2.73 GB/s | Caddy 2.84 GB/s; nginx 2.49 GB/s | Rough parity, but every client was saturated |
| Trivial WSGI, 64 connections | 44.1k req/s | Gunicorn `gthread` 6.50k req/s | Preserve; validate beyond a micro-app |
| WSGI with 10 ms blocking wait | 6.18k req/s | Gunicorn 5.66k req/s | Both approach the workload ceiling |
| Trivial ASGI, 64 connections | 33.9k req/s | Uvicorn asyncio/h11 11.9k req/s | Preserve; add accelerated and realistic cohorts |
| ASGI with 10 ms nonblocking wait | 5.31k req/s | Uvicorn 4.04k req/s | Scheduler is already credible for this probe |

This is a working baseline, not a release claim. The run recorded a dirty source
tree because the new benchmark harness and documentation were not committed, and
some servery rows had 15–30% min-to-max trial spread. Large-file clients consumed
96–100% of their available CPU, and nginx's connection-churn client was also
saturated. Phase 0 must produce a clean, lower-noise baseline before setting
regression thresholds.

## Current implementation hypotheses

The code suggests plausible causes, but none should be called a bottleneck until
profiles and controlled A/B experiments confirm it.

### Small static responses

Every request currently traverses the stdlib handler lifecycle, parses and
normalizes the URL, resolves a real filesystem path for symlink-safe containment,
opens and stats the file, computes validators and response headers, and then uses
`socket.sendfile()` regardless of whether the body is 1 KiB or 8 MiB. `sendfile`
is excellent for large files, but its setup and the separate header/body writes
may cost more than one bounded read plus one write for very small files.

Likely research targets are:

- handler and `BaseHTTPRequestHandler` dispatch overhead;
- URL translation and `realpath`/containment cost;
- open/stat/fstat and MIME/validator work repeated on hot files;
- header-list construction and multiple Python method calls;
- `sendfile` versus buffered or gathered writes by body size;
- GIL scheduling and context switching across many connection threads.

### Connection churn

The default `ThreadingHTTPServer` creates a thread and handler instance per TCP
connection. That is simple and portable, but a new-connection-per-request workload
pays thread creation, buffered stream setup, teardown, and scheduler activity on
every response. The existing `--max-workers` executor can reuse threads, but each
keep-alive connection occupies one worker for its lifetime and the option has not
yet been evaluated as the default static backend.

### Large files

Plain HTTP/1.1 already sends regular files with `sendfile`; TLS and unsupported
platforms use bounded userspace copies. The comparison did not reveal a server
bottleneck because the Python client saturated first. The correct near-term action
is better measurement and regression protection, not a new transfer algorithm.

### WSGI and ASGI

The minimal paths are already fast. WSGI materializes list/tuple results and emits
headers plus body in one write. ASGI uses a purpose-built asyncio HTTP/1.1 parser
and response state machine. Their gaps are ecosystem breadth, process lifecycle,
and production assurance more than trivial dispatch speed.

## Rules for the work

1. **Measure first.** A profile identifies candidates; a controlled A/B benchmark
   decides whether a change is useful.
2. **Never trade away safety silently.** Path containment, framing, validators,
   ranges, atomic writes, and body disposal remain correctness invariants.
3. **Keep size-dependent choices automatic initially.** Operators should express
   memory/resource policy, not tune syscall trivia. Diagnostic switches may exist
   during research but should not automatically become public CLI flags.
4. **Preserve the large-file fast path.** Small-response improvements must not
   replace `sendfile` globally.
5. **Separate runtime cohorts.** Normal-GIL, free-threaded, multi-process, and
   optional native-accelerated competitors get separate results.
6. **Do not optimize only the benchmark fixture.** Add realistic static and
   dynamic cases before accepting a change.
7. **Count complexity.** A 5% result that duplicates the entire HTTP stack is a
   poor trade. Prefer changes that improve clarity or isolate complexity behind a
   small interface.
8. **Retain a portable fallback.** Linux-specific acceleration may be optional,
   but Windows and macOS must keep correct, tested behavior.

## Roadmap overview

Effort ranges are rough engineering time for research plus a reviewed design; they
are not delivery promises.

| Phase | Rough effort | Primary output | Decision gate |
| --- | --- | --- | --- |
| 0. Trustworthy baseline | 1–2 weeks | Clean, repeatable comparison artifacts | Client headroom and acceptable variance |
| 1. Explain the static gap | 1–2 weeks | CPU/syscall/allocation/off-CPU profiles | At least 80% of hot cost assigned to components |
| 2. Low-risk static experiments | 2–4 weeks | A/B prototypes for I/O, metadata, headers, pools | One or more changes clear benefit/risk gates |
| 3. Connection architecture spikes | 3–6 weeks | Thread-pool, selector, and process prototypes | Choose, defer, or reject each architecture |
| 4. Implement selected static path | 3–6 weeks | Production-quality chosen design | Static targets met with no protected regression |
| 5. Dynamic and runtime validation | 2–4 weeks | Broader WSGI/ASGI matrix and compatibility gates | Microbenchmark wins survive realistic workloads |
| 6. Production-origin capabilities | Multi-release | Supervisor/reload/observability/trust controls | Production-origin checklist satisfied |
| 7. Protocol and deployment tiers | Ongoing | TLS/h2/h3, cold-cache, and network evidence | No unsupported generalization from HTTP/1 loopback |

Phases 0–2 should be completed before committing to an event-driven rewrite or
multi-process supervisor. Phases 5–7 can overlap after measurement infrastructure
is stable.

## Phase 0 — establish a trustworthy baseline

### 0.1 Re-run from immutable inputs

- Commit or otherwise snapshot the exact servery source and benchmark harness.
- Use image digests, not only mutable image tags.
- Record kernel, CPU model/governor, container runtime, filesystem, mount type,
  mitigations, CPython build flags, and GIL state.
- Disable unrelated scheduled work and verify stable power/thermal behavior.
- Keep one-server-CPU results as the architectural baseline; add separate 2- and
  4-CPU scaling cohorts.

**Gate:** JSON reports `git_dirty: false`, exact image digests, and all expected
provenance fields.

### 0.2 Establish client headroom

- For small responses, increase client processes until another increase changes
  server throughput by less than 3% while client CPU remains below 85%.
- Cross-check the Python client with one mature native load generator. Use the
  same request headers and validate the response independently before timing.
- For large files, use a separate machine or a native body-discarding client on a
  fast link. Loopback plus Python body draining is currently the bottleneck.
- Record client-side errors, ephemeral-port pressure, socket limits, and packet
  retransmits.

**Gate:** no headline row is published when the client is saturated. Saturated
rows may remain explicitly labeled lower bounds.

### 0.3 Reduce statistical noise

- Use at least seven trials of 15 seconds for candidate decisions.
- Report median plus min/max, median absolute deviation, and confidence interval
  or bootstrap interval—not only a point estimate.
- Randomize or balanced-rotate implementation order, with a cooldown if thermal
  drift is observed.
- Run on at least two Linux machines before changing defaults; run portability
  smoke tests on macOS and Windows.
- Define a practical noise floor. A provisional rule is that changes below 5%
  are inconclusive unless repeated evidence is much tighter.

### 0.4 Expand the workload axes without mixing them

Keep each as a separate labeled cohort:

- file sizes: 0 B, 1 KiB, 16 KiB, 64 KiB, 1 MiB, 8 MiB, 1 GiB;
- concurrency: 1, 8, 32, 64, 256, capped where client/resource policy requires;
- connection mode: keep-alive, fixed requests/connection, one request/connection;
- cache state: warm page cache, controlled cold cache in a disposable VM;
- runtime: CPython 3.15 normal-GIL and a supported free-threaded build;
- transport: plain HTTP/1.1 first; TLS, HTTP/2, and HTTP/3 later;
- response policy: identity, compression, conditional `304`, and byte range.

**Deliverable:** a checked-in baseline description and machine-specific JSON
artifacts retained outside git.

## Phase 1 — explain the small-static and churn gaps

### 1.1 CPU and off-CPU profiles

Collect both Python and system evidence under `static-1k` at concurrency 1 and 64,
plus `static-churn-1k` at concurrency 32:

- sampling Python stacks without instrumentation-heavy deterministic profiling;
- Linux `perf` call graphs and hardware counters;
- off-CPU/futex/scheduler profiles for thread contention and wakeups;
- per-process context switches, migrations, run-queue delay, and GIL-visible
  behavior on normal and free-threaded builds;
- matching nginx and Caddy profiles at the system-call/scheduler level for scale,
  not for line-by-line implementation imitation.

Measure profiles with and without `--max-workers` at worker counts 8, 32, and 64.
The question is whether thread reuse helps churn enough to justify the connection
occupancy and queueing behavior.

### 1.2 System-call decomposition

Use syscall counts and timings to assign per-request cost to:

- `accept`, socket setup, and close;
- thread creation/teardown and synchronization;
- URL decoding and path translation;
- `realpath`/symlink resolution;
- `stat`, `open`, `fstat`, and close;
- header writes and `sendfile`/`send` calls;
- logging checks and time/date generation.

Run the same hot file repeatedly and a high-cardinality file set. A cache that
only helps one path should not be mistaken for a general request improvement.

### 1.3 Allocation and resident-memory decomposition

- Use `tracemalloc` for relative Python allocation sources while recognizing its
  measurement overhead.
- Sample cgroup RSS, PSS, anonymous memory, file cache, and thread count/stacks.
- Distinguish virtual thread-stack reservation from resident memory.
- Identify per-connection reader/writer buffers and handler objects that remain
  live during keep-alive.
- Measure memory at concurrency 1, 64, 256, and idle-keep-alive saturation.

### 1.4 Stage-level microbenchmarks

Create directly callable benchmarks for the actual production functions:

- request-line/header parsing;
- URL-to-contained-path resolution;
- hot and cold metadata/validator construction;
- MIME and compression eligibility decisions;
- response-head assembly;
- small body delivery using alternative write strategies;
- handler construction and teardown.

Microbenchmarks diagnose; only the external end-to-end suite accepts a change.

**Phase gate:** produce a cost model that explains at least 80% of CPU time and
most additional memory/context switches. If it cannot, improve instrumentation
before designing a new backend.

## Phase 2 — low-risk, reversible static-path experiments

Each experiment lives on a small branch or behind an internal diagnostic mode.
Every result includes throughput, tail latency, CPU/request, allocations/request,
RSS, syscalls/request, correctness tests, and code-complexity impact.

### 2.1 Size-aware body delivery

Compare these paths across the file-size sweep:

1. current headers followed by `sendfile`;
2. bounded `read()` plus separate header/body writes;
3. bounded `read()` plus one combined header-and-body write;
4. scatter/gather (`sendmsg`) where supported, with a portable fallback;
5. cached small immutable bytes, under an explicit aggregate byte budget.

Questions to answer:

- At what size does `sendfile` reliably beat a buffered write on each OS/runtime?
- Does one combined write materially improve 1–16 KiB latency?
- Does allocation/copy cost erase the win at concurrency 64 or 256?
- How do file replacement/truncation during a request affect each path?
- What aggregate memory budget prevents many concurrent small reads from becoming
  an amplification problem?

**Likely design if supported:** retain `sendfile` above an automatically selected
small-file threshold and use a bounded buffered path below it. Put the maximum
buffered-response budget in `Config`; keep syscall selection internal unless
operators demonstrate a real need to override it. A value of zero should force
streaming/sendfile for memory-constrained deployments and tests.

### 2.2 Response-head construction and write batching

- Measure the existing `send_response`/`send_header` method chain against the
  shared byte-oriented head builder.
- Prototype a static response plan that computes status, headers, framing, and
  body strategy once, then writes them through one narrow adapter.
- Cache only stable metadata fragments; `Date`, authorization, range,
  conditional, CORS, and per-request headers must remain correct.
- Test header ordering only where standards/clients care; do not preserve
  accidental ordering at a large performance cost.

**Gate:** accept only if the design reduces duplicated HTTP logic or provides a
clear end-to-end gain. Do not create a second untested static implementation.

### 2.3 Safe metadata and path caching

Evaluate three levels independently:

- extension-to-MIME/encoding metadata (low invalidation risk);
- file stat/validator metadata keyed by canonical path plus inode/mtime/size;
- resolved containment/path results (highest security and invalidation risk).

Required adversarial tests include symlink swaps, rename-over-file, atomic
replacement, deletion/recreation, directory permission changes, case-folding,
Windows drive/UNC behavior, and a path changing from contained to escaping.

Possible policies:

- no cache, current safe baseline;
- short TTL plus bounded entry count for metadata only;
- event-driven invalidation where the OS supports it, with polling/fallback;
- pre-opened directory descriptors and anchored lookup on capable Unix systems;
- an explicit `static_metadata_cache_entries`/byte budget, default off until
  cross-platform evidence is strong.

**Hard gate:** no cache is accepted if a stale entry can bypass containment or
serve a replaced file under an old authorization/validator decision. Performance
does not justify weakening this invariant.

### 2.4 Reused worker threads

Benchmark the existing executor path as an implementation candidate, not just a
limit:

- worker counts below, equal to, and above active connection counts;
- keep-alive durations and idle clients;
- churn rates and overload behavior;
- queue-slot limits and rejection latency;
- memory and shutdown behavior.

A pool may substantially improve churn by avoiding thread creation, but a
keep-alive connection occupies a worker even while idle. If adopted, expose
worker count and bounded queue/admission policy; do not hide queueing behind an
apparently harmless performance default.

### 2.5 Small caches and precompressed assets

- Evaluate serving validated `.gz`/`.zst` sidecars before adding more runtime
  compression work.
- Benchmark a bounded hot-body cache separately from the existing compressed-body
  cache.
- Key by canonical path, inode/mtime/size, representation coding, and relevant
  policy; serialize cache misses to avoid stampedes.
- Measure invalidation, eviction CPU, memory fragmentation, and high-cardinality
  workloads.

Keep every cache disabled or conservatively bounded outside a measured profile.
The `cdn` profile is the natural place for an opt-in static-origin policy.

### Phase 2 acceptance gate

Promote an experiment only when all are true:

- at least 10% end-to-end improvement outside the measured noise floor, or a
  smaller improvement paired with a clear reduction in code/allocations;
- no more than 5% regression on protected large-file, WSGI, ASGI, TLS, range,
  conditional, upload, and listing workloads;
- no increase in timed errors or protocol/conformance failures;
- bounded and documented memory behavior;
- cross-platform fallback and free-threaded tests pass;
- the setting represents operator policy if made public.

## Phase 3 — connection architecture spikes

Low-risk tuning may not close a 6–7x event-server gap. Prototype alternatives
before deciding whether the remaining gap is an acceptable consequence of the
product's simplicity.

### 3.1 Bounded threaded backend

Turn the existing pool into a deliberately designed backend:

- fixed reusable workers;
- bounded accepted/queued connections;
- explicit idle keep-alive timeout and maximum requests/connection;
- fast overload rejection with metrics;
- graceful drain and shutdown;
- identical request parser and response plan.

This is the lowest-complexity architecture and remains the portability baseline.
The request-count and idle sub-policies are now implemented independently of
selecting the pool: `0` requests is unlimited, the `cdn`/`app` profiles select
1,000, terminal responses explicitly close, and an optional keep-alive timeout
can release an idle connection before the active timeout. The pooled backend
itself remains rejected as a default because its churn gain came with materially
worse p99; bounded queueing, overload metrics, full phase deadlines, and graceful
drain remain open design work.

### 3.2 Selector-based HTTP/1 frontend

Build a time-boxed static-read-only spike using `selectors` or a minimal asyncio
frontend. It should reuse shared parsing, containment, response planning, and
body-delivery abstractions rather than fork feature semantics.

Research questions:

- How much of the small-static gap disappears when idle connections no longer
  own threads?
- Can `stat`/open and other blocking filesystem work be kept off the event loop
  without recreating a large worker subsystem?
- Can plain-socket `sendfile` integrate with backpressure and partial writes?
- What is the cancellation/timeout/state-machine burden?
- How much feature parity is required for uploads, WebDAV, ranges, compression,
  proxying, and TLS?
- Does Windows selector/proactor behavior require a materially different backend?

Potential configuration is `connection_backend = threaded|selector|auto`, with
threaded remaining default until the selector backend passes the entire HTTP/1
conformance corpus and shows a large, repeatable benefit.

### 3.3 Multi-process scale-out

Prototype the smallest viable supervisor separately from the connection backend:

- 1/2/4 workers on 1/2/4 CPUs under normal-GIL CPython;
- equivalent free-threaded one-process tests;
- listener sharing/inheritance or `SO_REUSEPORT` where appropriate;
- worker crash/restart, readiness, drain, and forced termination;
- memory cost per worker;
- behavior of compression caches, target-write locks, WebDAV locks, ACME state,
  rate counters, and access logs.

Use `workers = 1|N|auto` only after defining shared-state semantics. A supervisor
that scales reads but silently breaks write exclusion is not an acceptable
performance feature.

### Architecture decision record

For threaded pool, selector, and multi-process prototypes, record:

- benchmark improvement and scaling efficiency;
- p99 under overload and slow clients;
- resident memory and file-descriptor/thread/task counts;
- platform coverage;
- feature and conformance parity;
- security-review surface;
- implementation and maintenance size;
- interaction with the zero-dependency constraint;
- whether the remaining gap is acceptable within servery's direct-edge release
  objectives or requires a different stdlib architecture.

**Phase gate:** choose at most one new default architecture. Other useful modes
must justify their ongoing test matrix; otherwise reject or defer them.

## Phase 4 — implement and stabilize the selected static design

### Provisional performance targets

Reset exact targets from the clean Phase 0 baseline. The following are directional
goals on the one-server-CPU HTTP/1 baseline:

- improve 1 KiB static throughput by at least 50% and cut p99 by at least 30%;
- improve 32-connection churn throughput by at least 75% and halve p99;
- move within roughly 1.5x of Caddy on the small-static/churn cases if this is
  achievable without an event-driven rewrite of the entire product;
- retain at least 95% of clean-baseline large-file throughput;
- keep static cgroup peak memory at or below the current level at equal load;
- preserve WSGI/ASGI throughput within 95% of baseline;
- retain zero errors and all functional/security/conformance gates.

nginx parity is not a mandatory goal. Its C implementation and event-driven
architecture serve a different optimization point. A transparent residual gap is
preferable to unsafe caching or a permanently duplicated protocol stack.

### Implementation requirements

- Add direct unit tests for every new strategy and differential wire tests against
  the existing implementation.
- Extend the external scenario matrix to cover every decision threshold.
- Add benchmark artifacts before/after on the same clean host.
- Document automatic thresholds, resource budgets, platform fallbacks, and
  relevant profile defaults.
- Retain diagnostic modes long enough to bisect regressions, then remove switches
  that are implementation trivia rather than useful policy.
- Update the capability-gap document with the actual result, including rejected
  ideas and why they were rejected.

Checkpoint: representation digests now satisfy the opened-resource ownership
boundary in production HTTP/1 and the benchmark selector. Hashing streams rather
than allocating the whole file; concurrent same-key requests share one transient
result; selector workers, queue slots, and retained entry count are distinct
policies. Production intentionally exposes none of those new knobs yet and
retains zero entries. Before promotion, test sequential/high-cardinality access,
large identities, mutation invalidation, SHA-512, slow storage, multi-CPU scale,
and HTTP/2/3 parity.

## Phase 5 — validate and productionize dynamic serving without chasing noise

### 5.1 Broaden WSGI workloads

Add identical applications that exercise:

- small materialized responses and streaming generators;
- 1 KiB, 64 KiB, and 1 MiB request/response bodies;
- many response headers and cookies;
- blocking waits and CPU-bound work;
- early return without consuming a request body;
- exception paths and access logging;
- representative Flask/Django applications in benchmark-only environments.

Compare servery with Gunicorn `sync` where semantics permit and `gthread` where
keep-alive/blocking concurrency is required. Separate one-worker dispatch cost
from multi-worker process-management results.

Checkpoint: identical 16-chunk 64 KiB and 1 MiB generator responses and a
body-consuming 64 KiB POST are now in the external harness, and the current
fixture is mounted into candidate and prebuilt baseline containers to prevent
app-code drift. The POST cohort protects the opt-in total body deadline. 1 KiB
and 1 MiB request bodies, headers/cookies, CPU work, frameworks, and exception/
logging cohorts remain open.

### 5.2 Broaden ASGI workloads

Add:

- portable Uvicorn asyncio/h11 and labeled `uvloop`/`httptools` cohorts;
- streaming request and response bodies;
- 100/1,000/10,000 concurrent nonblocking waits;
- disconnect/cancellation and slow consumers;
- WebSocket echo, fanout, long-lived idle connections, and backpressure;
- representative Starlette/FastAPI applications in benchmark-only environments
  (**basic JSON/stream/validation closed; broader middleware/tasks remain**);
- HTTP/2-to-ASGI only if servery intends to support it.

Optional native competitor acceleration must never leak into servery's runtime
dependency set. It is comparison evidence, not a core dependency proposal.

Checkpoint: identical 16-event 64 KiB and 1 MiB responses now run against
servery and Uvicorn. Servery drains every intermediate event before accepting the
next, closing the unbounded producer-ahead buffering gap. The 64 KiB paired gate
was neutral; 1 MiB loopback results remain client/noise limited. In the
four-client 64 MiB slow-reader gate, distinct producer allocations peaked at
40.4 MiB with intermediate drains versus 283.5 MiB with only the final drain, an
85.7% reduction; paired request rate moved -0.4% and all samples had zero errors.
A blocked intermediate drain is directly cancellable, and the write-progress
timeout is now cross-transport policy. A post-body `receive()` now observes real
peer EOF without consuming pipelined bytes; cancelled listeners restore the
original stream protocol hooks. Five-trial keep-alive and churn gates are neutral
(+4.1%/-0.3% RPS and +0.1%/-0.3% p99 respectively). Response completion now
also wakes lazy request-scope listeners without closing the persistent stream;
its keep-alive gate is neutral and churn direction is too dispersed to claim.
Closed-send `OSError` behavior now matches advertised ASGI HTTP 2.4 within the
protected budget. Response event ordering, exact response framing, incomplete
application return, and negotiated HTTP/1 trailers are also closed. Large
streaming requests, field-registry trailer policy, and broader framework
behaviors remain open. Native Uvicorn and 100/1,000-client wait scaling are now
closed in the labeled Python 3.14 tier; 10,000 remains an infrastructure gate.
Basic framework compatibility is now demonstrated: pinned Starlette JSON and
16-event streaming pass on CPython 3.15, while pinned FastAPI success and exact
`422` validation pass on CPython 3.14. In five-trial same-app comparisons,
servery is 72–96% higher RPS than portable Uvicorn for the Starlette cohorts and
35–63% higher for FastAPI, with lower p99 throughout. FastAPI on 3.15 remains an
ecosystem gate: Pydantic Core 2.46.4 has no cp315 wheel, so the lean image
deliberately does not hide that by installing a compiler/Rust toolchain. Native
Uvicorn acceleration has the same runtime split: current uvloop 0.22.1 and
httptools 0.8.0 publish cp314/cp314t wheels but no cp315 wheels. The labeled
Python 3.14 cohort therefore pins portable Uvicorn to asyncio/h11 and native
Uvicorn to uvloop/httptools. For immediate 1 KiB ASGI at 64 connections,
servery is +134.6% RPS versus portable Uvicorn but -54.8% versus native. For a
10 ms nonblocking wait, servery moves from +10.1% to -12.6% at 100 clients and
from +168.5% to -33.9% at 1,000 when the comparison changes from portable to
native. All accepted samples have zero errors. The loopback 10,000-client
probes are rejected for ranking because transport retries and
client/ephemeral-port limits remain active even with persistent warmup and
ramping; that tier requires a dedicated or source-sharded load generator.
The combined Python 3.14 framework/native image now passes Starlette JSON,
16-event `StreamingResponse`, FastAPI JSON, and exact FastAPI `422` validation
under servery, portable Uvicorn, and native Uvicorn. In five-trial throughput
gates, servery remains +52.6% to +100.4% RPS versus portable Uvicorn but is
-26.2% to -49.9% versus native Uvicorn, with zero errors and essentially equal
per-framework memory. Native acceleration is comparison evidence only; it does
not enter servery's zero-dependency runtime. Background tasks, exception
middleware, SSE, multipart limits, framework WebSockets, and
long-duration recycling remain open.
ASGI lifespan startup/shutdown semantics and state propagation are now closed.
The default `auto` mode preserves unsupported-callable compatibility, `on`
requires lifecycle support, and `off` removes lifecycle/state work. Explicit
startup failure or timeout prevents bind; shutdown failure/timeout is surfaced;
successful lifespan state is shallow-copied into HTTP and WebSocket scopes.
Unsupported/off requests avoid the copy. Five-trial minimal, Starlette, FastAPI,
and `off` source gates are neutral with zero errors. Bounded draining of active
HTTP/WebSocket tasks and post-response background work before lifespan shutdown
is now closed for the single-process threaded HTTP/1, HTTP/2, ASGI HTTP, and
WebSocket runtimes. Cancellation-resistant application code still requires the
planned supervisor's process-termination boundary.
Strict ASGI HTTP/1 field syntax is also closed. The opt-in 32-field cohort
protects parser-cost scaling; ordinary and header-heavy throughput remain at the
5% protected boundary. Large cookies, near-limit heads, high-cardinality names,
and proxy differential corpora remain open.

### 5.3 Add compatibility and operational gates

- Run WSGI validation and a framework compatibility corpus.
- Track supported ASGI spec versions/extensions explicitly.
- Test disconnect delivery, streaming, trailers, and WebSocket protocol edges.
  (**lifespan failure/state and single-process active-task drain are closed;
  broader framework/interoperability coverage remains open**)
- Measure startup, graceful shutdown, memory leaks over long runs, and request
  recycling—not only requests/second.

**Decision:** preserve the current fast engines if realistic results hold. Spend
engineering effort on conformance, observability, worker lifecycle, and proxy
trust before micro-optimizing an already fast trivial response.

## Phase 6 — close the direct production-edge capability gap

This track is partly independent of raw speed. Sequence it so performance
architecture does not have to be redesigned repeatedly.

### 6.1 Continuous assurance and observability

Start early because every later phase needs it:

- fuzz HTTP framing, HTTP/2, WebSocket, multipart, archive, WebDAV, and ACME
  inputs;
- add bounded-cardinality request/error/latency/connection/worker/cache metrics;
- add liveness/readiness and structured operational events;
- expose overload, queue, restart, renewal, and shared-state failures;
- define a security-response and supported-version policy.

### 6.2 Deployment identity and configuration

- trusted-proxy CIDRs and safe `Forwarded`/`X-Forwarded-*` parsing, default none;
- Unix-domain sockets and systemd socket activation where supported;
- validated TOML configuration with `--check-config` and secret indirection;
- effective non-secret configuration/provenance at startup;
- privilege-drop and hardened service/container examples.

### 6.3 Supervisor and reload

After Phase 3 defines workers and shared state:

- crash detection and bounded restart backoff;
- readiness before traffic;
- graceful drain with configurable deadline;
- zero-downtime config/certificate replacement;
- optional request-count/age recycling;
- clean Windows strategy rather than a Unix-signal-only claim.

### 6.4 Admission and traffic policy

- per-client/global request and bandwidth limits;
- bounded queues and expensive-operation budgets;
- total header and body-read phase deadlines (write-progress stalls are now bounded);
- fair scheduling between small requests and large transfers;
- production profile defaults, with generous/off defaults for LAN use.

Checkpoint: maximum requests per HTTP/1 connection is implemented as an
independent policy. The general default is unlimited to avoid forced reconnect
and TLS cost; the `cdn`/`app` profiles use 1,000. An independent optional
keep-alive idle timeout is also implemented; unset inherits the active timeout
and no profile shortens it without workload evidence. Neither setting is a total
header/body deadline, rate limit, worker-recycling supervisor, or HTTP/2/3 stream
budget. An independent opt-in write-progress timeout now bounds stalled socket
writes, asyncio drains, and HTTP/3 capacity waits across transports. It resets
after progress and is deliberately not a total response-duration or minimum-rate
policy, so the rest of this section remains open.

### 6.5 Bounded direct-edge scope

The product target now is a directly exposed, single-service edge. Continuous
ACME renewal, safe overload behavior, worker supervision, graceful replacement,
and operational status therefore belong in the production gate. General proxy
pools, health-checked upstream retries, disk proxy caching, circuit breaking,
and multi-site traffic management remain deferred: they substantially expand
state and security obligations without being necessary for the selected first
release. No deferred feature creates a dependency on another edge server.

The detailed feature list and tradeoffs remain in
[Production capability gap](production-capability-gap.md).

## Phase 7 — protocol and real-deployment tiers

After the common HTTP/1 baseline is understood, add fair cohorts for:

- TLS 1.3 steady-state records, full handshakes, and resumed sessions;
- HTTP/2 multiplexed small and large streams;
- HTTP/3 under latency and packet loss;
- gzip/Zstandard with matched content and compression policy;
- ranges and conditional `304` responses;
- cold filesystem cache and high-cardinality trees;
- slow readers, aborted downloads, and constrained bandwidth;
- separate-client-host 1/10/25 GbE deployment;
- reverse-proxy origin mode with trusted deployment headers.

Each tier needs shared semantics and a suitable client. Do not append unlike
protocol results to the plaintext ranking.

## Experiment record template

Every research branch should leave a short record containing:

```text
Hypothesis:
Affected scenarios and protected scenarios:
Implementation or prototype:
Configuration/public API impact:
Correctness and security invariants:
Host/runtime/image provenance:
Throughput and latency before/after:
CPU, syscall, allocation, and memory evidence:
Trial dispersion and client headroom:
Cross-platform/free-threaded results:
Complexity and maintenance cost:
Decision: accept / revise / reject / defer
Reason and follow-up:
```

Rejected experiments are useful results. Record them so the project does not
revisit attractive but unproductive ideas without new evidence.

## Stop and rollback rules

Reject or revert a change when any of these apply:

- the gain is within the established noise floor;
- containment, framing, atomicity, or validator correctness becomes weaker;
- large-file or dynamic performance regresses beyond the protected budget;
- memory becomes unbounded or scales unexpectedly with file size/concurrency;
- the change adds a third-party core runtime dependency;
- a platform silently falls back to incorrect or materially different behavior;
- operational configuration cannot express the new resource tradeoff clearly;
- maintenance/security surface is disproportionate to the remaining gap;
- a mature edge proxy solves the requirement more safely and simply.

## Recommended first three work packages

1. **Baseline and profiles:** complete Phase 0 and the CPU/syscall/off-CPU parts of
   Phase 1. This replaces hypotheses with a cost model.
2. **Small-file I/O crossover:** prototype size-aware buffered/combined writes
   versus `sendfile`, including aggregate memory budgets. This is the most focused
   path to the 1 KiB gap and does not endanger the existing large-file design.
3. **Churn backend comparison:** benchmark the existing bounded executor, then
   time-box a selector-based static spike. Decide whether a 2–3x residual gap is an
   accepted product tradeoff before designing a permanent second backend.

Only after these packages should the project commit to a new connection backend
or multi-process supervisor. The benchmark suite defines the evidence; the
[External server comparison benchmarks](server-comparison-benchmarks.md) document
defines how to collect it; and
[Reliability and performance priorities](reliability-performance-priorities.md)
defines the resource-policy rules that every implementation must preserve.

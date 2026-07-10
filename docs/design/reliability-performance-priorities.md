# Reliability and performance priorities

Status: implemented for servery 1.5.0. Recorded 2026-07-10 against 1.4.0, then
implemented and verified before the 1.5.0 release. The "Current concern" sections
below preserve the pre-change diagnosis; the implementation record immediately
below is the source of truth for the shipped behavior.

The review found a strong baseline: the full quality gate passes, the HTTP/1.1
file path uses `sendfile` where it helps, uploads and archives are normally
streamed, and the project has unusually broad protocol coverage for a pure-Python
server. The remaining risks are concentrated in cross-protocol resource policy,
concurrent writes, and places where an intentionally small first implementation
now needs a more explicit production boundary.

## Decision rules

Not every limit should become a flag, and not every buffered operation should be
rewritten as a stream. Use these rules when implementing this backlog:

1. **Protocol correctness is not configurable.** Ambiguous request framing,
   keep-alive desynchronization, invalid HTTP/2 stream transitions, resource
   ownership, and atomic no-overwrite behavior must always be correct.
2. **Resource policy should usually be configurable.** Connection counts,
   transfer counts, memory thresholds, listing size, compression cache size, and
   compatibility modes vary by machine and use case. Give them conservative
   defaults and keep CLI and library behavior aligned.
3. **Keep the fast small-response path.** A single `read()` plus one write is often
   faster than a streaming state machine for a small file. Use bounded hybrid
   strategies rather than making every response stream.
4. **Bound aggregate cost, not only one request.** A 10 MiB allocation looks safe
   until 100 requests make it a 1 GiB allocation. Limits need to compose across
   transports and concurrent clients.
5. **Do not expose implementation trivia prematurely.** Chunk sizes and scheduler
   quanta should remain internal until benchmarks show operators need to tune
   them. Expose thresholds and budgets that describe observable resource policy.
6. **Compatibility shortcuts must be named honestly.** If a mode deliberately
   relaxes a protocol guarantee to interoperate with a client, make it explicit,
   documented, and observable at startup.

The likely shape is a small resource-policy object derived from `Config`, rather
than an ever-growing collection of unrelated module constants. Public names below
are proposals, not frozen API. A setting should first be available through the
library configuration; add a CLI flag when it is useful to an operator, not merely
because the implementation has a number in it.

## Priority overview

| Priority | Issue | Correctness invariant or configurable policy? |
|---|---|---|
| 1 | HTTP/2 and HTTP/3 buffer complete files | Hybrid policy: bounded buffering threshold + streaming |
| 2 | Request-body framing and cleanup differ by adapter | Correctness invariant; body-size policy configurable |
| 3 | HTTP/2 does not track active stream state completely | Correctness invariant; concurrent-stream limit configurable |
| 4 | HTTP/3 lifecycle differs from the documented transport model | Explicit configurable mode and port; no silently ignored combinations |
| 5 | Connection and transfer budgets are not system-wide | Configurable budgets with bounded network-facing defaults |
| 6 | Concurrent writes can race | Correctness invariant; conflict/wait/cleanup policy configurable |
| 7 | Access-log instances share global mutable handlers | Correctness and lifecycle invariant; buffering optional later |
| 8 | WebDAV advertises locks that it does not enforce | Explicit compatibility mode versus enforced mode |
| 9 | Large directory responses do too much work before pagination | Configurable limits and detail levels |
| 10 | Compression work is repeated and performance comparison is manual | Configurable cache/buffering policy + measured regression workflow |

## 1.5.0 implementation record

| Priority | Shipped decision and evidence |
| --- | --- |
| 1 | `_response.ResponseBody` keeps `bytes` for files at or below `max_buffered_response` (1 MiB by default) and returns `FileBody` above it. HTTP/2 schedules bounded file reads against both flow-control windows; HTTP/3 reads in worker threads and bounds queued QUIC stream bytes. HTTP/1.1 retains its `sendfile`/streaming path. A zero threshold forces streaming. |
| 2 | `_body.parse_framing` is the shared duplicate-length and transfer-encoding policy. Sync adapters reject chunked bodies; ASGI decodes them incrementally. `max_request_body` and `keepalive_drain_limit` are configurable, and every early/unread path either drains exactly or closes. Raw pipelining tests prove rejected or ignored bodies cannot become a second request. |
| 3 | HTTP/2 tracks active streams from HEADERS through response completion, advertises and enforces `max_h2_streams`, applies active `INITIAL_WINDOW_SIZE` changes, validates stream IDs/CONTINUATION/window arithmetic, budgets rapid resets and CONTINUATION/SETTINGS/PING floods, and writes with a non-recursive fair scheduler. Protocol-negative and large/small interleave tests cover the state machine. |
| 4 | `http3`, `http3_only`, and `http3_port` are part of `Config`. Normal `--http3` starts TCP and UDP together, shares certificate material and the compression cache, advertises the actual live UDP port through `Alt-Svc`, and shuts both down under one owner. Real aioquic tests cover streaming, fallback, advertisement, and clean stop. |
| 5 | `max_connections` defaults to 256 across threaded HTTP, ASGI, and HTTP/3; `max_workers`, `max_h2_streams`, and `max_tftp_transfers` remain distinct policies. Saturation rejects immediately and recovery is tested. TFTP netascii is stateful and streaming across read/block boundaries. |
| 6 | `_writecoord.TargetLocks` serializes canonical targets across multipart, resumable PUT, WebDAV, archive entries, and TFTP. `write_lock_timeout` controls reject-versus-wait; writes and extraction commit from same-directory temporary files. `partial_upload_ttl` expires stale sidecars lazily and `max_partial_uploads` bounds their count after a lazy first-use inventory. Concurrent no-overwrite and resumable tests prove one winner without corruption. |
| 7 | Each `AccessLog` owns and closes one handler; construction occurs only after successful handler setup and bind. Independent-server, close-order, failed-bind, and end-to-end tests cover ownership. |
| 8 | `dav_lock_mode` is `class1`, `compat`, or `enforced` (default for writable DAV). Enforced mode stores exclusive depth-infinity locks, refreshes/expires tokens, and checks affected ancestors/descendants on writes. Compat warns at startup. Read-only DAV honestly advertises class 1. |
| 9 | `max_listing_entries`, `listing_page_size`, and `listing_details_threshold` bound HTML work; truncation is reported and expensive metadata/metrics degrade above the threshold. `max_propfind_entries` returns explicit `507` rather than a partial `207`. |
| 10 | `max_compress_size` and a byte-bounded `compression_cache_size` control compression. The cache is keyed by canonical path, mtime, size, coding, and level; miss computation is serialized to prevent hot-file stampedes. The `cdn` profile enables a 32 MiB cache. Weekly/manual benchmarks retain per-request and concurrent throughput, p50/p95/p99, errors, and RSS evidence. |

Defaults are conservative compatibility choices, not protocol rules. Operators can
trade memory for latency with the buffering/cache thresholds and trade reuse for
body-drain work with `keepalive_drain_limit`; framing, state transitions, atomic
commit, and log ownership are never configurable relaxations.

### 1.5.0 verification snapshot

The pre-release Linux run on 2026-07-10 produced the following evidence. Absolute
performance numbers are machine-specific; the scheduled workflow retains comparable
JSON for trend analysis.

- The complete quality gate passed: ruff, ty, Bandit, 666 functional tests (six
  environment-specific skips), 90% combined line/branch coverage, wheel/sdist build,
  and the zero-runtime-dependency check.
- The full 663-test matrix before the final three HTTP/2 abuse cases passed on
  CPython 3.14 and on the 3.13t and 3.14t free-threaded builds with the GIL
  disabled. The final 17-case HTTP/2 conformance module then passed on all three;
  Python 3.14 also passed its 90% coverage gate.
- The 21-case benchmark suite and the separate real-aioquic HTTP/3 benchmark passed.
  Strict MkDocs, workflow security, distribution metadata, installed-wheel HTTP,
  and single-file/zipapp version smokes also passed.

| Concurrent HTTP/1.1 scenario | Throughput | p99 | Sampled RSS delta | Errors |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB file, 1,000 requests / 16 workers | 2,682 req/s | 7.04 ms | 3.5 MiB | 0 |
| 8 MiB file, 64 requests / 16 workers | 3.29 GB/s | 29.20 ms | 2.3 MiB | 0 |
| 50-entry listing, 1,000 requests / 16 workers | 717 req/s | 17.88 ms | 5.7 MiB | 0 |

The in-process load client drains responses in 64 KiB chunks, so its own whole-body
allocations do not swamp the server memory signal. The RSS scope still includes both
server and client workers and should be interpreted as a trend delta, not isolated
server-process accounting.

| Directory entries | Render time | Python peak allocation | Page bytes |
| ---: | ---: | ---: | ---: |
| 1,000 | 23.8 ms | 6.1 MiB | 468,743 |
| 10,000 | 150.4 ms | 8.1 MiB | 470,853 |
| 100,000 | 388.7 ms | 21.8 MiB | 396,566 |

## 1. Use a hybrid buffered/streaming response path

### Current concern

`_response.build_static()` reads an entire file into one `bytes` object for the
HTTP/2 and HTTP/3 backends. HTTP/2 then slices that object into DATA frames, and
HTTP/3 builds and queues the response from the QUIC event callback. Peak memory is
therefore proportional to the sum of all concurrent file sizes, and a file read or
compression operation can block unrelated HTTP/3 connections on the event loop.

### Tradeoffs

| Strategy | Advantages | Costs and limits |
|---|---|---|
| Buffer with `read()` | Few Python calls; known `Content-Length`; easy conditional/compression path; excellent small-file latency | Memory grows with file size and concurrency; delays first byte; blocks the HTTP/3 loop if done inline |
| Plain-socket `sendfile` | Lowest CPU and copy cost; excellent large-file throughput | HTTP/1.x plaintext only in the current design; not usable through TLS or HTTP/2/3 framing |
| Bounded read/write streaming | Predictable memory; early first byte; works through TLS and framed transports | More reads, writes, allocations, and state; can reduce small-response throughput |
| `mmap` | Avoids an explicit full Python copy in some cases | File-lifetime and truncation hazards, address-space pressure, and no direct solution for TLS/framing; not the preferred default |

### Recommended direction

Keep three paths:

- HTTP/1.1 plaintext, full identity response: retain `sendfile`.
- Small responses: retain a buffered fast path up to a measured
  `max_buffered_response` threshold. A starting candidate is 1 MiB, but select it
  from benchmarks on GIL and free-threaded builds rather than treating 1 MiB as a
  specification.
- Larger TLS, HTTP/2, and HTTP/3 responses: stream from a file handle in bounded
  chunks. HTTP/2 must schedule chunks against connection and stream windows;
  HTTP/3 should yield control and respect the QUIC implementation's backpressure.

Generated listings and error responses can remain buffered because their bounded
size is needed for an exact `Content-Length`. A threshold of `0` may be useful in
library configuration to force streaming during tests or on memory-constrained
systems. Do not make the I/O chunk size a CLI option without benchmark evidence.

### Acceptance criteria

- Concurrent downloads have a measured aggregate-memory ceiling independent of
  the files' total size.
- HTTP/3 file I/O and compression do not run synchronously on its event callback.
- Small-file latency does not materially regress from the buffered baseline.
- HTTP/2 and HTTP/3 still produce exact validators, content lengths, HEAD
  behavior, and end-of-stream framing.

## 2. Centralize request framing and body disposal

### Current concern

The file handler, proxy, WSGI, CGI, ASGI, WebDAV, and upload code make separate
decisions about `Content-Length`, chunked encoding, maximum body size, and what to
do with unread bytes. Some paths clamp an oversized length and leave the remainder
on a persistent connection; a WSGI application may return without consuming its
input; several early error responses neither drain the body nor close the socket.
The unread bytes can then be parsed as another request.

### Correctness versus policy

These rules are invariants and must not be optional:

- Reject conflicting `Transfer-Encoding` and `Content-Length` framing.
- Reject conflicting duplicate `Content-Length` fields; identical duplicates may
  be normalized only if the chosen RFC policy permits it.
- Never replace an advertised length with a smaller length while retaining the
  connection.
- After a response, either consume exactly the accepted request body or close the
  connection.
- A body rejected before consumption gets a clear error and `Connection: close`.

The accepted size is policy. Separate `max_request_body` from
`max_upload_size`: dynamic applications and proxy requests need an input cap even
when file upload is disabled. Existing callers that rely on 100 MiB can preserve
that default during a compatibility release.

For an accepted WSGI body the server has two defensible choices when the app
returns early: drain the known remainder to preserve keep-alive, or close to avoid
spending time reading data the app did not want. A small configurable
`keepalive_drain_limit` can preserve reuse for small bodies while closing for a
large remainder. It is a resource policy, not a protocol relaxation.

### Acceptance criteria

- A shared framing test corpus runs against every HTTP/1 adapter.
- Oversized, duplicate-length, transfer-encoding-plus-length, and unread-body
  cases cannot desynchronize a follow-on request.
- ASGI may expose body chunks through `receive()` rather than assembling the full
  accepted body, preserving its expected backpressure model.

## 3. Complete the HTTP/2 connection state machine

### Current concern

The advertised concurrent-stream limit currently counts incomplete header blocks,
not all active streams. A stream leaves that count before its response is sent.
When output stalls on flow control, the window-pump path can process another
HEADERS frame and re-enter request dispatch recursively. Existing stream windows
also need correct adjustment when the peer changes `INITIAL_WINDOW_SIZE`.

### Recommended direction

Maintain an explicit stream record from accepted HEADERS through local and remote
end-of-stream. Validate client stream IDs, ordering, CONTINUATION sequencing,
closed-stream frames, reset budgets, and window arithmetic in that state machine.
The connection reader should enqueue work; a non-recursive writer scheduler should
fairly emit DATA for ready streams.

The correctness rules are fixed, while `max_h2_streams` is configurable. The
advertised SETTINGS value and the enforced value must be the same. Lower limits
are useful on memory-constrained machines; higher limits are useful only after the
streaming response path makes per-stream memory predictable.

Fair round-robin scheduling is a good initial policy. Do not expose scheduler
quantum or priority tuning before real workloads show a need.

### Acceptance criteria

- The active-stream limit covers header assembly, request handling, response
  streaming, and half-closed states.
- A client cannot create recursive dispatch depth by withholding WINDOW_UPDATE.
- Interleaved large and small streams demonstrate that one large response does
  not monopolize the connection.
- Protocol-negative tests cover stream-ID reuse, interleaved CONTINUATION,
  window overflow, and SETTINGS changes with active streams.

## 4. Make HTTP/3 an explicit part of the server lifecycle

### Current concern

The CLI currently chooses HTTP/3 *instead of* the normal `serve()` lifecycle, but
the transport design describes HTTP/3 as a UDP listener alongside the TCP fallback
and advertised through `Alt-Svc`. Because `http3` is not part of `Config`, option
validation cannot see it. ACME setup, TFTP startup, ASGI selection, self-signed
certificate material, and shutdown behavior can consequently be skipped or fail
in surprising combinations.

### Recommended direction

Add an explicit transport mode to configuration and run all listeners under one
startup/shutdown owner:

- `http3 = true` means HTTP/3 alongside the selected TCP server and enables
  `Alt-Svc` on TCP responses.
- `http3_only = true` is a separate expert mode when the operator deliberately
  wants no TCP fallback.
- `http3_port` permits UDP/TCP port differences where deployment requires them;
  the common default should use the advertised public port.
- Certificate acquisition or generation happens once before either listener
  starts, and both receive the same material.

Not every combination has to be supported. Unsupported combinations should fail
configuration validation with a precise message; no supplied feature flag should
be silently ignored. ACME documentation also needs one source-of-truth refresh:
several historical design sections still say it is unimplemented even though the
CLI and core now ship it.

### Acceptance criteria

- The TCP listener remains a fallback while HTTP/3 is enabled, unless
  `http3_only` is explicit.
- `Alt-Svc` matches the actual UDP listener and is absent when it is unavailable.
- ACME, user-provided certificates, and supported self-signed development mode
  have deterministic startup behavior.
- Configuration tests cover every meaningful transport combination.

## 5. Apply resource budgets across all transports

### Current concern

`max_workers` bounds the threaded HTTP server only. The default HTTP path remains
thread-per-connection, ASGI creates tasks without a corresponding connection
budget, and TFTP creates a thread for every request datagram. TFTP netascii mode
also converts an entire file in memory before sending it. Network-facing profiles
therefore do not share a coherent resource ceiling.

### Recommended direction

Use related but distinct limits:

- `max_connections`: accepted HTTP/TLS/ASGI/HTTP/3 connections or sessions.
- `max_workers`: blocking HTTP/1/WSGI/CGI work allowed concurrently.
- `max_h2_streams`: logical streams per HTTP/2 connection.
- `max_tftp_transfers`: active per-transfer sockets and workers.
- An aggregate or per-feature compression/archive budget if measurement shows
  CPU-heavy work can starve ordinary downloads.

One number should not control all of them: 100 idle async sockets are much cheaper
than 100 CGI children or active gzip jobs. Network-facing profiles should set
bounded defaults appropriate to their purpose, while the local profile can remain
more permissive for compatibility. A non-blocking capacity check is preferable to
blocking the accept loop indefinitely; overload behavior should be observable in
metrics/logs and should close or reject quickly.

Implement TFTP netascii as a stateful streaming converter so CR/LF translation can
cross read boundaries without materializing the file. A bounded executor or
semaphore should cover the entire TFTP transfer lifetime, including retries.

### Acceptance criteria

- A datagram or connection flood cannot create unbounded threads, tasks, sockets,
  or queued work.
- Limits are enforced under GIL and free-threaded builds.
- Saturation tests verify controlled rejection and recovery after capacity frees.

## 6. Serialize writes per target and finalize atomically

### Current concern

Multipart upload, resumable PUT, WebDAV PUT, archive extraction, and TFTP write
contain check-then-write sequences. Two requests can both observe a missing target
and later replace it even with overwrite disabled. Two resumable chunks can read
the same sidecar length and append concurrently, corrupting the upload.

### Recommended direction

Add a per-canonical-target lock registry covering the full decision, write, and
commit sequence. Under that lock, re-check target existence and resumable offsets
immediately before modifying state. Preserve the current `allow_overwrite` policy;
locking makes that choice reliable rather than replacing it.

Potential conflict policies are:

- `reject` (current safe default): return 409/412 immediately.
- `wait`: wait up to a small `write_lock_timeout`, then reject.
- `overwrite`: existing explicit behavior.
- A future `rename` policy may choose a unique name, but should not be introduced
  implicitly because it changes the URL/file contract.

An in-process lock prevents races among servery request handlers. It cannot stop an
unrelated local process from changing the directory. Cross-process no-replace
rename is not portable in the Python standard library, so document that boundary;
where possible, use exclusive file creation or a hidden lock file to narrow it.

Resumable sidecars also need configurable lifecycle policy, such as
`partial_upload_ttl` and a cap on outstanding partial bytes/files. Cleanup must
never remove a sidecar whose target lock is held.

### Acceptance criteria

- Concurrent no-overwrite uploads produce one winner and explicit conflicts, not
  last-writer-wins replacement.
- Concurrent resumable chunks cannot overlap, duplicate, or corrupt the sidecar.
- Failure and cancellation leave either the old complete file or a valid partial,
  never a partially replaced destination.

## 7. Give each access log independent ownership

### Current concern

`AccessLog` instances mutate handlers on the process-global `servery.access`
logger. Constructing a second server can detach the first server's handler, and
closing one instance can close the other instance's destination. The log file is
also opened before server bind and dynamic-handler initialization are known to
succeed, so failed startup attempts can leak handles.

### Recommended direction

Each `AccessLog` should own exactly one handler or file object and close only that
object. Create it after the server has bound and all fallible handler setup has
succeeded, or protect construction with `ExitStack` so partial initialization
rolls back. Multiple server objects in one process must remain independent.

Synchronous `FileHandler` writes are a reasonable default: they are simple,
ordered, and durable enough for an ad-hoc server. A bounded queue is an optional
future performance mode for high request rates, but it needs an explicit policy
for blocking versus dropping and a counter/warning for lost records. Do not make
logging asynchronous merely to hide unmeasured filesystem latency.

### Acceptance criteria

- Two simultaneous servers log only to their own destinations and may close in
  either order.
- Failed bind, port scan, bad application import, and invalid CGI-root tests leak
  no descriptors or handlers.
- The test suite is clean under `ResourceWarning`-as-error for project-owned
  resources.

## 8. Make the WebDAV locking mode explicit

### Current concern

WebDAV advertises compliance class 2, reports supported locks, and returns an
exclusive token, but does not store or enforce it. This compatibility shortcut is
useful because some built-in clients will not mount read-write without class 2,
but it can mislead an editor into believing concurrent changes are protected.

### Tradeoffs and migration

Removing class 2 immediately would improve protocol honesty but break existing
Finder/Windows workflows. Keeping fake locks silently preserves mounting but risks
lost updates. Treat the behavior as a selectable compatibility policy:

- `dav_lock_mode = "class1"`: do not advertise locking; safest honest mode.
- `dav_lock_mode = "compat"`: current fake-token behavior, accompanied by a
  startup warning and explicit documentation that it provides no exclusion.
- `dav_lock_mode = "enforced"`: store lock root, depth, scope, token, owner, and
  expiry; validate lock tokens on every affected write.

For 1.x compatibility, introducing the setting with `compat` as the temporary
default may be less disruptive. Once `enforced` is implemented and exercised
against target clients, make it the default for `--dav-write`; a major release is
the clean point to reconsider the compatibility default. Read-only DAV does not
need to claim write-lock protection merely to remain readable.

An in-memory enforced lock table fits servery's single-process design but locks
vanish on restart. That is acceptable if documented and if clients receive clear
failures for stale tokens. Multi-process/shared-storage WebDAV remains outside the
project's lane.

### Acceptance criteria

- The advertised DAV class and live properties match the selected mode.
- Enforced mode covers PUT, DELETE, MKCOL, MOVE, COPY, and writes below a
  depth-infinity lock.
- Compatibility mode is never described as real mutual exclusion.

## 9. Bound large directory work before rendering

### Current concern

The HTML listing scans and stats up to 100,000 entries, then filters, sorts,
facets, and computes metrics before selecting a 1,000-row page. Pagination limits
HTML size but not most CPU, filesystem I/O, or memory. WebDAV `PROPFIND Depth: 1`
has no entry cap and builds the complete XML tree in memory.

### Recommended direction

Expose policy at the semantic level:

- `max_listing_entries`: maximum entries considered for an HTML listing.
- `listing_page_size`: rows rendered per page.
- `listing_details_threshold`: above this size, omit expensive facets/timeline or
  compute only basic name/type data.
- `max_propfind_entries`: maximum children in a depth-one WebDAV response.

The HTML page can visibly report truncation and invite the user to filter or raise
the limit. WebDAV cannot silently return a partial collection as if complete;
rejecting an over-limit operation with an explicit resource error is safer than an
incomplete `207` unless a client-compatible continuation protocol is designed.

For name-sorted listings, it may be possible to select the requested page before
statting every entry, but size/date sort and full-directory metrics inherently need
metadata. A short TTL cache can help repeated browsing, but directory mtime does
not change when a child's size or mtime changes. Any cache must therefore accept a
documented freshness window or retain enough child identity to revalidate it.
Start with bounded work and degraded details before adding a complicated cache.

### Acceptance criteria

- Time and peak memory are measured at 1k, 10k, and 100k entries.
- Pagination actually reduces expensive per-entry work where the selected sort and
  detail mode allow it.
- HTML truncation is visible; WebDAV never reports a silently incomplete success.

## 10. Reuse compression work and automate useful performance evidence

### Current concern

Compressible files up to 10 MiB are read and compressed for every request. That is
fast enough for occasional sharing but wastes CPU and memory for repeated CDN-like
traffic. The benchmark suite is broad, but regression comparison is an explicit
local command and its main transport figures measure single-connection latency,
not aggregate memory, tail latency, or overload behavior.

### Recommended direction

Use the same hybrid decision as the response path:

- Small files may be compressed into memory in one operation.
- Large accepted files should use incremental `gzip`/zstd compression or fall back
  to identity when preserving `sendfile` is cheaper.
- A byte-bounded compressed-representation cache can reuse hot static responses.
  Key it by canonical path, `mtime_ns`, size, coding, and compression settings;
  bound it by total bytes rather than entry count.
- Compression level, maximum compressible size, and cache byte budget are valid
  library policies. Add CLI flags only for settings an operator is likely to tune.

Caching trades memory for CPU and latency. Defaulting the cache to zero preserves
today's minimal-memory behavior; a modest cache may be appropriate in the `cdn`
profile. Streaming trades some compression ratio and call overhead for bounded
memory and earlier first byte. Benchmarks should choose the thresholds rather than
assuming one strategy wins at every size.

Performance validation should have two tiers:

- Deterministic CI tests for bounded memory behavior, deadlock/race freedom, gross
  latency failures, and overload recovery.
- Scheduled or dedicated-runner benchmarks that retain artifacts for throughput,
  p95/p99 latency, CPU, and peak RSS. Hosted-runner noise makes a strict PR gate on
  small percentage changes unreliable; use trend reporting or generous thresholds
  until a stable runner exists.

### Acceptance criteria

- Repeated hot-file requests demonstrate the expected CPU/latency improvement
  without exceeding the configured cache budget.
- Concurrent compression cannot multiply memory beyond the aggregate policy.
- Benchmark artifacts include concurrency, tail latency, and peak memory, not only
  median single-request latency.

## Suggested implementation order

The priorities above are risk-ranked, but their implementation has dependencies:

1. Centralize request framing/body disposal and repair access-log ownership. These
   are contained correctness fixes with little architectural dependency.
2. Add per-target write serialization and the deterministic concurrency tests.
3. Introduce the shared resource-policy vocabulary and bounded TFTP execution.
4. Build the streaming response abstraction, then use it from HTTP/2 and HTTP/3.
5. Replace HTTP/2 recursive flow handling with the explicit stream scheduler.
6. Move HTTP/3 under the unified listener lifecycle and reconcile transport docs.
7. Add directory limits/detail degradation and the compression cache/streaming
   policy, guided by the expanded benchmark evidence.
8. Add real WebDAV lock enforcement, retaining an explicit compatibility mode for
   clients that require the historical behavior.

Each change should update `Config`, CLI help where applicable, the relevant guide
and design document, tests, and benchmark evidence in the same commit. Do not mark
an item complete because its happy-path unit test passes: the completion evidence
for these items is bounded behavior under concurrency and agreement between every
transport that claims the feature.

# Production-shaped selector prototype — 2026-07-10

Status: continue as a benchmark-only architecture candidate; do not expose a
public backend. Connection realism retains the churn and tail-latency signal, but
small-static throughput misses the roadmap target and feature parity remains far
from production.

## What changed from the ceiling spike

`benchmarks/comparison/selector_prototype.py` is a separate adapter so the
original minimal spike remains an honest ceiling control. The prototype adds:

- one connection object owning incremental parser state and post-head bytes;
- correct dispatch of pipelined heads, with declared body bytes never re-parsed;
- strict shared request-line, Host, field, framing, and error-status policy;
- bounded admission with immediate rejection and recovery counters;
- a total request-head deadline plus independent keep-alive and write deadlines;
- maximum requests per connection and explicit terminal close;
- bounded asyncio backpressure and chunked sendfile progress when a write
  deadline is configured;
- explicit accepted/rejected/completed/error/timeout/cancellation counters;
- graceful listener close, active-task drain, forced cancellation, and abort.

The graceful-drain test found an asyncio lifecycle trap: awaiting
`Server.wait_closed()` before cancelling owned client tasks can wait on those
same callbacks on newer runtimes. The accepted order is stop admission, drain or
cancel the explicitly registered task set, then await listener closure.

The prototype still supports only plaintext static GET/HEAD. Filesystem
containment/open/fstat and MIME work remain synchronous on the event loop. It has
no TLS, uploads, WebDAV, proxying, access logs, styled errors, or public
configuration. Generated listings now exist only through the explicit bounded
worker/queue policy documented in the
[listing experiment](2026-07-10-selector-listings.md). Representation digests
exist as an explicit bounded-worker research capability, documented in
[Opened-identity digests and bounded selector hashing](2026-07-10-selector-digests.md),
and are disabled without worker policy. It now redirects directories and serves
contained indexes, and shares
single-range, `If-Range`, and conditional `304` selection with production. A
request body forces close after one response rather than being consumed. These
remaining omissions prohibit promotion regardless of benchmark results.

## Correctness and failure-mode gates

Direct tests cover:

- two pipelined requests delivered in one read;
- request-count terminal close before a later pipeline;
- declared body bytes not becoming a request;
- duplicate Host serialized as `400` through the shared parser;
- total slowloris head timeout;
- saturated admission rejection and recovery;
- forced cancellation after the graceful-drain deadline;
- a non-reading 32 MiB transfer released by write timeout.

The prototype remains in the external Docker harness as an explicit server name;
it is never part of default comparisons or the shipped CLI.

## Connection-model result

CPython 3.15.0b3, one server CPU, isolated clients, five rotated three-second
trials, zero timed errors:

| Workload | Production | Ceiling spike | Production-shaped prototype |
| --- | ---: | ---: | ---: |
| 1 KiB, 64 keep-alive | 18.73k RPS / 13.80 ms p99 | 21.65k / 3.87 ms | 20.12k / 4.03 ms |
| 1 KiB, 32 churn | 6.49k RPS / 7.32 ms p99 | 10.28k / 4.16 ms | 10.51k / 4.27 ms |

Against production, the realistic prototype is +7.4% RPS / -70.8% p99 for
keep-alive and +62.0% / -41.7% for churn. Median peak memory was 25.5 MiB and
26.7 MiB versus production's 30.4 MiB and 29.5 MiB. Churn RPS dispersion was
high (16.4% MAD), so the magnitude needs a longer clean-host run before a
promotion decision.

Relative to the ceiling spike, policy/state cost about 7% keep-alive throughput
and little tail latency; churn medians were effectively equal within much wider
dispersion. Connection realism does not erase the architecture signal, but the
current production server's accepted static improvements mean the original +50%
small-static target is no longer met by this prototype.

## Body crossover and memory result

With the existing 16 KiB buffering policy, the prototype was neutral at 16 KiB
but 21% behind production at 64 KiB. The original spike showed the same 64 KiB
direction, isolating the loss to asyncio sendfile at this crossover rather than
the new policies. The prototype therefore exposes the existing
`small_file_buffer_size` policy internally for controlled comparison; no new
public knob was invented.

| Body/concurrency | Prototype policy | Change versus production | p99 change | Peak memory |
| --- | --- | ---: | ---: | ---: |
| 16 KiB / 64 | buffer through 16 KiB | -1.3% RPS | -73.5% | 26.7 vs 32.7 MiB |
| 64 KiB / 64 | sendfile above 16 KiB | -21.4% RPS | -65.3% | 27.7 vs 30.5 MiB |
| 64 KiB / 64 | buffer through 64 KiB | +2.1% RPS | -69.5% | 27.8 vs 30.7 MiB |
| 64 KiB / 256 | buffer through 64 KiB | +18.5% RPS | -76.2% | 28.6 vs 39.1 MiB |
| 8 MiB / 16 | sendfile | client-limited parity | client-limited | 83.6 vs 83.4 MiB |

The 64 KiB buffer increases the theoretical transient body budget to 4 MiB at
64 active sends and 16 MiB at 256, but measured cgroup peak remained below the
threaded server in both runs. This is evidence for a selector-specific crossover
study, not permission to change the production default: TLS, other platforms,
high-cardinality files, and sustained slow consumers still need gates.

Ignored artifacts under `benchmarks/artifacts/`:

- `selector-prototype-2026-07-10.json`;
- `selector-prototype-size-sweep-2026-07-10.json`;
- `selector-prototype-64k-control-2026-07-10.json`;
- `selector-prototype-buffer-64k-2026-07-10.json`;
- `selector-prototype-buffer-64k-c256-2026-07-10.json`.

## Decision and next gate

Continue, but keep the backend benchmark-only. The next acceptance work is:

1. move or budget blocking filesystem acquisition without recreating an
   unbounded worker queue;
2. share regular-file conditionals and ranges rather than duplicating them;
3. test abort/truncation/replacement during partial sendfile and buffered sends;
4. run macOS/Windows and TLS fallbacks plus a longer low-noise churn gate;
5. define feature routing explicitly—silent loss of uploads, WebDAV, proxy, or
   directory behavior is not acceptable.

Stop if filesystem offload erases the tail benefit or semantic convergence turns
the prototype into an independently maintained second server. In that case the
honest product boundary remains threaded servery behind Caddy/nginx.

The first item now has a measured decision: inline acquisition remains the warm
default; a cancellation-safe bounded executor is retained only as explicit
slow-storage research policy. See
[Selector filesystem offload — 2026-07-10](2026-07-10-selector-filesystem-offload.md).

The second and third items are now partly closed. Shared opened-identity
selection covers conditionals and single ranges, while replacement and
truncation tests prove original-handle bytes and transfer-error abort behavior.
The selector retains much lower p99, but a range reaches production throughput
only with the explicit 64 KiB buffer control; `304` remains 5–10% lower in
throughput. See
[Shared conditional and range selection — 2026-07-10](2026-07-10-shared-range-selection.md).

Directory/index work is also partially closed: redirect construction and
contained index discovery are shared, cache policy is explicit, and a missing
listing implementation originally returned `501` rather than masquerading as
`404`; the follow-on bounded listing planner now closes that gap with explicit
worker, queue, and render limits. The
production index refactor is neutral; selector p99 is 72.6% lower while its RPS
ratio is too dispersed for a claim. See
[Shared directory redirect and index selection — 2026-07-10](2026-07-10-shared-directory-index.md).
See [Shared listing policy and bounded selector rendering — 2026-07-10](2026-07-10-selector-listings.md)
for the follow-on generated-page result.

Download disposition and SPA fallback are now also shared. SPA is an explicit
disabled-by-default prototype policy, and both the requested path and fallback
index must pass containment. Download throughput is neutral versus production
with 73.3% lower p99; SPA throughput is noisy while p99 is 71.1% lower. See
[Shared download disposition and SPA fallback — 2026-07-10](2026-07-10-shared-download-spa.md).

Compression is now a benchmark-only explicit capability. Coding/ETag/range
semantics and the shared cache are reused; cache misses run under a bounded
worker/queue and own duplicated descriptors across cancellation. Production
uncached gzip improves 91.2% RPS through shared transient single-flight; selector
remains 65.3% faster with 78.7% lower p99. See
[Selector compression and transient single-flight — 2026-07-10](2026-07-10-selector-compression.md).

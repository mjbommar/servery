# Static response planning boundary

Status: design research, 2026-07-10. This is the next architecture step after the
[selector frontend spike](performance-experiments/2026-07-10-selector-spike.md),
not an accepted public API.

## Why this boundary matters

The selector spike materially improves small-file throughput and tail latency,
but promoting its originally hand-written parser and response builder would
create a third HTTP implementation. Servery already has two static paths:

- `ServeryHandler` implements the complete HTTP/1 behavior and opens files before
  deriving metadata (`open` then `fstat`);
- `_response.build_static` plans the reduced HTTP/2 and HTTP/3 behavior. At the
  start of this pass it used `stat` followed later by `open`/read; the first
  implementation checkpoint below now gives streaming bodies an owned handle.

A permanent selector frontend should share semantics with HTTP/1, not add another
reduced path. The immediate design task is therefore a transport-neutral response
plan with explicit resource ownership.

## Current semantic inventory

| Concern | HTTP/1 `ServeryHandler` | `_response` used by HTTP/2/3 | Required shared behavior |
| --- | --- | --- | --- |
| URL translation | stdlib normalization plus realpath containment | `safe_join` in each transport | one tested normalization contract; containment always revalidated before acquisition |
| File identity | `open` then `fstat` | now `open` then `fstat` | validators, size, and bytes derived from one opened identity |
| Directories | redirect, index lookup, archive/selection, bounded listing | redirect and bounded listing only | capabilities explicit; no accidental transport drift |
| SPA fallback | supported | absent | common decision or explicitly unsupported by backend capability |
| Ranges / `If-Range` | supported | absent | one range plan over the selected representation |
| Conditional `304` | supported | supported | one validator implementation and header policy |
| `?download` | supported | absent | common disposition decision |
| Representation digest | supported for identity files | absent | common, with blocking hash work scheduled deliberately |
| Compression | streaming path plus shared cache | buffered/cached path | common coding/ETag decision; transport-specific emission |
| Small body policy | configurable 16 KiB plaintext read threshold | `max_buffered_response`, default 1 MiB | preserve separate resource policies unless evidence supports merging them |
| Large body | owned open handle returned to HTTP/1 sender | now an owned handle transferred to h2/h3 | an owned file lease, offset, and exact count |
| Policy headers | handler injection | byte-list builder | one logical header policy, encoded by the transport |
| Error pages | styled HTTP/1 pages | minimal numeric bytes | decide deliberately; do not claim parity where it does not exist |

This inventory rules out simply calling `_response.build_static` from HTTP/1. It
would change directory/index/range behavior and increase the default buffered
body from 16 KiB to 1 MiB. It also rules out letting a selector backend depend
directly on `BaseHTTPRequestHandler` state.

## Implementation checkpoint: opened HTTP/2 and HTTP/3 bodies

The first ownership slice is implemented. `_response.build_static` opens a file,
derives validators and size with `fstat`, and either reads/closes it for a bounded
body or transfers the same handle in `FileBody`. HTTP/2 reads and closes that
handle across flow-control windows; HTTP/3 reads it in worker calls and closes it
on success, HEAD, cancellation, or error. Connection teardown and stream reset
remain idempotent close paths.

A replacement-race test opens the original file, atomically replaces its path,
and proves the planned content length and streamed bytes still refer to the
original identity. HTTP/2/3 conformance and `ResourceWarning`-as-error tests pass.
This removes one reopen and a metadata/body mismatch without adding public
configuration. It does not make path containment atomic against a symlink swap;
anchored acquisition remains separate research.

## Implementation checkpoint: shared acquisition facts

Stage A now has an executable differential contract for overlapping HTTP/1 and
HTTP/2/3 file semantics. Stage B's first slice is also implemented:
`_static.open_file` returns one slots-based `FileBody` containing the open handle,
`fstat`, content type, coding, ETag, and Last-Modified value. HTTP/1 consumes the
same facts while retaining its existing feature-complete response adapter.

A seven-trial rotated image A/B found median paired changes of +1.9% throughput /
+2.3% p99 for keep-alive and −0.03% throughput / +1.3% p99 for churn, within the
protected budget and substantially smaller than host-wide time-separated drift.
The full method and dispersion are recorded in
[Shared static representation plan — 2026-07-10](performance-experiments/2026-07-10-shared-static-plan.md).

## Target model

Use three layers rather than one all-purpose builder.

### 1. Request facts

A small request view contains only facts needed by response policy:

```text
method, normalized target, selected headers, TLS state, backend capabilities
```

HTTP/1 parsing remains responsible for framing, connection persistence,
`Expect: 100-continue`, and body disposition. HTTP/2 and HTTP/3 continue to
validate their own framing. The shared view must not pretend those protocols have
the same connection state machine.

### 2. Acquired resource

Filesystem lookup returns an owned resource, not just a path:

```text
Missing | Directory(path) | OpenFile(handle, canonical_path, stat)
```

For a file, the same open handle supplies `fstat`, validators, range bytes, and
the eventual body. Ownership transfers exactly once to the body emitter, which
must close it on success, cancellation, timeout, client abort, or planning error.
The implemented HTTP/2/3 lease removes their former stat/open identity race.
Directory lookup remains separate because listing/index/archive work has
different cost and
invalidation behavior.

On capable Unix systems, a later implementation may acquire relative to a
pre-opened root descriptor. That is an internal lookup strategy, not a reason to
weaken the cross-platform containment contract or add an `openat2` syscall via
`ctypes` prematurely.

### 3. Response plan

A pure planner consumes request facts, configuration, and the acquired resource:

```text
ResponsePlan(status, logical_headers, body, connection_policy)

body = Empty | Bytes(data) | OpenFileSlice(lease, offset, count)
```

Logical headers use text names/values with validation at construction. HTTP/1
encodes canonical wire lines; HTTP/2/3 lowercase and compress them. A file slice
never stores only a path: it owns the opened identity whose metadata produced the
headers. Compression may replace the identity body with bounded/cached bytes,
but that transition is explicit in the plan.

Transport emitters decide partial-write, flow-control, frame, `sendfile`, TLS,
and cancellation mechanics. They do not recompute status, validators, ranges,
content coding, or security policy.

## Blocking work and the selector

Warm-cache `realpath`/open/stat is fast but still blocking. Moving it blindly to
a generic executor can reintroduce the worker queue and tail-latency problems the
selector is meant to remove. The production-shaped prototype should compare:

1. synchronous lookup on the loop with a strict per-iteration work budget;
2. a small bounded filesystem executor with immediate overload rejection;
3. an OS-anchored lookup strategy where the standard library exposes safe
   primitives, retaining the portable path elsewhere.

Directory rendering, archives, digest hashing, compression misses, uploads,
WebDAV, WSGI, and CGI are not event-loop work. The simplest credible first
selector backend may support static GET/HEAD only and hand other accepted
connections to the threaded backend, but only if protocol detection and
connection ownership remain unambiguous. A public mode that silently drops
features is not acceptable.

## Configuration boundary

Configuration should express operator policy, not syscall trivia.

- Reuse `small_file_buffer_size`; zero continues to force streaming/sendfile and
  bounds transient memory for constrained deployments.
- Reuse `max_connections`, active `timeout`, and the optional HTTP/1
  `keepalive_timeout` across backends.
- Reuse the implemented `max_requests_per_connection` policy. Zero is unlimited;
  a positive value is enforced by threaded/WSGI/ASGI HTTP/1 and must compose with
  the selector state machine without changing its meaning.
- Add total header, body-read, and write-progress deadlines only when they have
  distinct observable semantics; do not overload the request count or idle
  timeout to stand in for every phase.
- Add a bounded filesystem-work queue/worker count only if measurements show it
  is required and overload behavior is defined.
- Keep digest hashing in its own bounded work class when a selector supports it.
  Worker count, queue slots, and retained entry count express different resource
  policies; the prototype measures them separately and production retains no
  digest entries.
- Keep send/sendfile/scatter-gather selection internal.
- Do not expose `connection_backend=selector` until conformance and feature scope
  are honest. An experimental library-only entry point is preferable during the
  prototype phase.

## Incremental migration plan

### Stage A — differential contract tests

Build a table-driven corpus for GET/HEAD covering files, empty files, index
directories, redirects, missing and escaping paths, symlink swaps, conditionals,
ranges, compression, cache headers, CORS/security headers, `?download`, SPA, and
file replacement/truncation. Compare logical plans where features overlap and
record intentional protocol/backend differences.

Gate: no current HTTP/1 wire behavior changes.

Status: the overlapping regular-file, conditional, compression, redirect, and
missing/escaped-path corpus is implemented. Shared identity-selection tests now
cover full, bounded/suffix/unsatisfiable ranges, `If-Range`, and `304`; selector
wire tests cover GET/HEAD, replacement/truncation ownership, contained indexes,
download disposition, opt-in SPA fallback, full-identity digests, and generated
listings with bounded worker failure/cancellation behavior. Expand the corpus
before migrating styled errors or write/dynamic feature routing.

### Stage B — opened-file plan in HTTP/1

Extract only the regular-file portion of `_serve_file` into an opened-resource
planner. Keep the existing handler as adapter and retain its current body sender.
Do not migrate directories, uploads, or archives in the first patch.

Gate: full HTTP/1 conformance, installed-wheel smoke, cross-platform fallback,
free-threaded tests, and external results within the 5% protected budget.

Status: acquisition and representation facts are shared; HTTP/1 emission remains
unchanged. Conditional and single-range selection now also share one primitive,
with a header-free fast path in the handler. The paired external gate passes but
the header-heavy result is near the budget (-4.3% range and -4.2% `304` RPS), so
it is a maintainability/correctness decision rather than a performance claim.
Full repository, free-threaded, packaging, and native CI gates remain required
after each further slice.

### Stage C — converge HTTP/2 and HTTP/3

The schedulers now own an open lease rather than reopening a path. Continue by
moving shared validator, coding, policy-header, and range decisions into the plan
in small reviewable steps. Preserve each protocol's flow control and cancellation.

Gate: h2/h3 conformance, slow/aborted stream tests, file replacement races, and
bounded memory at high stream counts.

### Stage D — incremental HTTP/1 parser

Lift the existing fast line/header/framing rules into an incremental parser that
can return `need more`, `complete`, or a precise error without socket I/O. The
threaded handler adapts its buffered reader first. Only then does a selector own
the parser state directly.

Gate: the complete request-parsing, smuggling, timeout, and pipelining corpus
passes against both adapters.

Status: the request-head parsing and policy slices are implemented.
`_request.parse_request_line` owns HTTP version validation, HTTP/0.9 behavior,
close defaults, error timing, and leading-`//` collapse. `RequestHeaders` and the
specialized blocking `read_headers` adapter preserve the threaded hot path, while
`HeaderBlockParser` accepts arbitrary fragments, returns post-header bytes, and
enforces the same line/count, first-wins, field-syntax, and obs-fold rules. Tests
exercise request-line errors, every header and whole-request split point, EOF
without a blank line, leftovers, limits, and post-completion misuse.
`RequestHeadParser` composes those pieces with shared body framing and produces
connection-persistence and `Expect` policy. The threaded handler consumes the
same finalizer through its fast buffered adapter; the selector consumes the
incremental parser.

The shared policy also closes the previously documented `FR-HOST-01` gap:
HTTP/1.1 requests with a missing, duplicate, or invalid `Host`, whitespace before
a field colon, invalid field-name bytes, or control bytes in a value receive
`400` and cannot reuse the connection. This strictness is intentionally not
configurable because permissive intermediaries and origins create request
desynchronization risk. The optimized strict checks cost 3.4% throughput / +3.0%
p99 in the longer keep-alive gate and 2.6% / +3.0% under churn: a deliberate
standards cost inside the protected 5% budget. A Python per-byte version that
regressed keep-alive throughput 12.5% was rejected.

An 11-trial longer paired keep-alive gate found +2.9% throughput and −2.7% p99;
the seven-trial churn gate found −0.3% throughput and −2.0% p99. These are neutral
architecture results, not performance claims. Details and the initial noisy run
are recorded in [Shared HTTP/1 request parser — 2026-07-10](performance-experiments/2026-07-10-shared-request-parser.md).

The request-head follow-up was also neutral: +1.0% throughput / −4.7% p99 for
keep-alive and −1.1% / +1.5% for churn in a seven-trial paired run. Body-byte
consumption/draining, pipelined-head ownership, deadlines, and error serialization
still need a multi-request connection state object before Stage D is complete.

The first independent connection-state policy is now implemented:
`max_requests_per_connection` is unlimited at `0`, closes explicitly on the
terminal response when positive, and stops dispatch of a later pipelined request.
It applies to threaded/WSGI/ASGI HTTP/1; the `cdn` and `app` profiles select 1,000.
The disabled path was neutral in the final paired gate (+2.2% static / −0.8% WSGI
throughput, +0.4% / −1.0% p99). Details are in
[HTTP/1 request-count connection policy — 2026-07-10](performance-experiments/2026-07-10-connection-policy.md).

The idle occupancy slice is also implemented. `keepalive_timeout=None` inherits
the existing active timeout; a positive value releases a dormant threaded/WSGI/
ASGI HTTP/1 connection sooner without changing active body/response policy. The
first ASGI implementation shape was rejected at −6.5% throughput; selecting the
alternate loop once per configured connection resolved the default path to −2.4%
throughput / +1.6% p99 at c64, with a longer c1 run showing no regression. See
[HTTP/1 keep-alive idle timeout — 2026-07-10](performance-experiments/2026-07-10-keepalive-timeout.md).

An attempted ASGI migration to `RequestHeadParser` was rejected: optimized
byte-native adapters still cost 18–19% on the minimal ASGI workload, and a later
whole-block field validator cost 14.2%. ASGI keeps its fast specialized parser,
but now enforces Host cardinality/authority and the 100-field budget directly;
that narrower slice passed at −0.7% throughput / +4.0% p99 at 64 connections and
−2.2% / +1.6% in the longer concurrency-one gate. Strict non-Host field syntax
remains an explicit semantic gap. Stage D's shared-parser claim is limited to the
threaded and selector adapters.

### Stage E — production-shaped selector prototype

Add admission, deadlines, partial writes, backpressure, sendfile progress,
cancellation, graceful drain, and metrics. Start with static read-only HTTP/1.
Benchmark keep-alive, churn, 16/64 KiB crossover, large files, slow readers,
aborts, and overload on Linux; run correctness/fallback tests on macOS, Windows,
normal-GIL, and free-threaded Python.

Gate: retain most of the measured selector ceiling, pass shared conformance, and
document unsupported features before considering any public backend setting.

Status: the first benchmark-only prototype is implemented. It explicitly owns
parser remainders and active tasks; enforces admission, total head/idle/write
deadlines, request-count close, cancellation, and graceful drain; and reports
bounded-cardinality lifecycle counters. Differential tests cover pipelines,
declared bodies, parser errors, slowloris, overload recovery, stalled writes, and
forced drain cancellation.

The result retains the churn/tail signal (+62.0% churn RPS, -41.7% p99) but not
the provisional +50% keep-alive target (+7.4% RPS, -70.8% p99). A 64 KiB
asyncio-sendfile crossover initially regressed throughput 21%; reusing the
existing buffer threshold at 64 KiB restored parity with lower measured memory,
including at 256 connections. The mode remains benchmark-only because several
major static semantics and platform gates are still absent. See the
[experiment record](performance-experiments/2026-07-10-selector-production-prototype.md).

The filesystem-offload follow-up rejects a generic pool as the warm-file default:
four bounded workers cost 29.8% keep-alive throughput and 13.3% churn throughput.
The cancellation-safe pool remains useful as explicit slow-storage policy; a
controlled 10 ms acquisition delay scaled nearly linearly from 98 inline RPS to
394/1,576 with 4/16 workers. Capacity is bounded, saturation is `503`, and late
results after cancellation are closed. Details are in
[Selector filesystem offload — 2026-07-10](performance-experiments/2026-07-10-selector-filesystem-offload.md).

The next semantic slice adds shared single-range, `If-Range`, and conditional
selection over the same opened identity. A 64 KiB buffer control brings the
selector's 1 KiB range throughput to -1.1% of production while reducing p99
74.7%; its default sendfile path remains 20.1% slower. Bodyless `304` is 5–10%
slower than production but about 70% lower at p99. Replacement and truncation
tests prove handle identity and abort/error accounting. This closes one semantic
gap without changing public configuration; details are in
[Shared conditional and range selection — 2026-07-10](performance-experiments/2026-07-10-shared-range-selection.md).

Directory canonicalization and contained index discovery are now shared without
moving archive or listing generation. Production ordering is unchanged and its
paired index gate is neutral (+1.2% RPS / -2.1% p99). The selector serves an
index with explicit cache policy, redirects missing slashes, and returns `501`
for its still-unsupported listing. Its indexed-file throughput result is noisy;
p99 remains 72.6% below production. See
[Shared directory redirect and index selection — 2026-07-10](performance-experiments/2026-07-10-shared-directory-index.md).

Download query parsing and safe disposition construction are now shared. SPA is
an explicit policy in both adapters and its root index is containment-checked;
an escaping symlink regression now returns `404`. The no-query production path
is neutral (-0.5% keep-alive / -0.8% churn paired RPS). Selector download is
throughput-neutral with 73.3% lower p99; SPA RPS is too dispersed for a claim and
p99 is 71.1% lower. Details are in
[Shared download disposition and SPA fallback — 2026-07-10](performance-experiments/2026-07-10-shared-download-spa.md).

Compression now reuses coding facts, the shared byte-bounded cache, and an owned
duplicate descriptor in a bounded selector worker/queue. The cache itself now
shares transient same-key results even with retention disabled and permits
distinct keys to compute concurrently. Production uncached gzip improves 91.2%
paired RPS / 24.8% p99; its warm-cache path is neutral. Selector uncached gzip is
another 65.3% faster with 78.7% lower p99. Details are in
[Selector compression and transient single-flight — 2026-07-10](performance-experiments/2026-07-10-selector-compression.md).

Access logging now has an explicit selector ownership model rather than an
event-loop-blocking file write or an unbounded background task. One writer thread
uses a bounded admission budget, `drop` or async `wait` overflow semantics, and
configurable batch size/window; accepted records drain before close. The fair
logged cohort audits delivered line count after timing. Selector `drop` finished
8.1% above current logged production RPS with 3.1% lower p99 and 1.2 MiB lower
peak memory, while all adapters delivered 100% of timed records. These controls remain
benchmark-only; see
[Bounded selector access logging — 2026-07-11](performance-experiments/2026-07-11-selector-access-logging.md).

## Stop rules

Stop or keep the selector experimental if shared planning materially complicates
the stable threaded path, blocking filesystem work erases the tail benefit,
Windows requires a separate implementation of comparable size, or feature parity
creates a second server that the project cannot review and maintain safely. In
that case, retain the accepted 16 KiB optimization and recommend Caddy/nginx for
edge concurrency; the benchmark result is still a useful product-boundary fact.

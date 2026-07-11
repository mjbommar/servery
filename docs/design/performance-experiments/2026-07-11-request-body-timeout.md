# Total HTTP/1 request-body timeout — 2026-07-11

Status: accepted as an opt-in HTTP/1 connection-occupancy policy. It is disabled
by default and no profile selects a value.

## Problem and policy semantics

The existing timeout settings answer different questions:

- `--timeout` bounds one active socket operation or ASGI body-read unit without
  progress;
- `--keepalive-timeout` bounds an idle HTTP/1 connection between requests; and
- `--write-timeout` bounds one response-write wait without progress.

A client can remain inside those policies while sending a request body slowly
but continuously. Threaded readers get a fresh socket-operation timeout as bytes
arrive; ASGI gets a fresh wait for each bounded body/chunk operation. Such a
client can retain a thread, task, admitted connection, application state, and
partial write target indefinitely.

`--request-body-timeout SECONDS` adds one **total** budget. Its clock starts on
the first nonempty body read and does not reset when bytes arrive. It includes
application pauses between body reads, because those pauses retain the same
request/body resources. The existing `--timeout` still applies: each blocking
read uses the shorter of its progress timeout and the remaining total budget.

The setting is deliberately optional. A total deadline is operator policy, not a
universally safe default: large uploads, slow WAN clients, applications that
process chunks between reads, and resumable upload strategies need different
budgets. Leaving it unset preserves existing behavior and avoids deadline state
on requests. Setting it to a positive value trades tolerance for a hard upper
bound on body-consumption occupancy.

Expiry aborts/closes the HTTP/1 connection rather than trying to reuse it or send
a late error over a partially received message. It is not a bandwidth floor, a
total application runtime limit, or a request-head deadline.

## Implementation boundary

Synchronous HTTP/1 body consumers share `_body.DeadlineReader`. It wraps the
existing buffered request stream only when the policy is enabled and a route
actually consumes a body. The wrapper:

1. starts its monotonic deadline lazily on the first read;
2. uses `BufferedReader.read1()` so one Python call cannot hide multiple socket
   waits that silently reset the deadline;
3. recomputes the remaining total before every underlying read;
4. retains any shorter active socket timeout; and
5. restores the previous socket timeout after each operation.

Its small internal buffer preserves `read()`/`readline()` behavior without
crossing the declared body boundary. Upload, resumable PUT, WSGI, CGI, reverse
proxy, and writable WebDAV paths all obtain their body source through the same
handler seam. Timeout exceptions bypass application/proxy/disk `500`/`502`
translation, clean temporary whole-file targets where required, and reach the
stdlib request loop's existing fail-closed timeout handling.

ASGI retains the original `_AsyncBody` class for the disabled policy and for
bodyless requests. Only an enabled request with declared or chunked content uses
`_TimedAsyncBody`, which caps each existing `wait_for()` by the absolute
remaining budget. The same deadline spans chunk events, chunk terminators, and
trailers. Timeout is computed before creating the read coroutine so expiry does
not leak an un-awaited coroutine.

There is no configuration for internal chunk size or wrapper selection. Those
are implementation details; only the total occupancy budget is observable
operator policy.

## Correctness evidence

End-to-end WSGI and ASGI tests use a 1-second progress timeout and a 150 ms total
body timeout. The client sends one of four body bytes every 70 ms. It therefore
makes progress well inside the ordinary timeout but is disconnected when the
total budget expires, before application dispatch can complete.

Unit tests additionally prove that:

- the blocking clock starts lazily;
- a socket's prior progress timeout is restored;
- expiration applies to already-buffered bytes after an application pause;
- `readline()` preserves bytes following its delimiter; and
- one ASGI deadline spans separately delivered chunk events and buffered trailer
  bytes.

The existing framing, body-size, exact pipeline ownership, ignored-body close,
chunked request, upload, proxy, CGI, WebDAV, WSGI, and ASGI suites protect normal
and failure behavior.

## Benchmark additions

The comparison harness now supports deterministic request bodies. Scenario
metadata stores an integer body size; probes and loadgen send `POST` with one
prebuilt repeated-byte payload and exact `Content-Length`. The mounted WSGI and
ASGI fixture consumes the complete body and returns the same validated 1 KiB
response.

Two opt-in scenarios are added:

- `wsgi-body-64k`; and
- `asgi-body-64k`.

Both use 32 connections, a 64 KiB request, a 1 KiB response, and identical app
semantics. They are disabled by default because request-body size distributions
are application-specific. The harness also exposes candidate-only
`--servery-request-body-timeout` so one image can compare enabled and disabled
policy without product or fixture drift.

## Rejected hot-path shapes

The first correct implementation reset a possible deadline reader in every
parsed threaded request and stored total-deadline fields in every ASGI body
object. A short frozen-image gate measured -14.0% static RPS and -6.2% ASGI RPS;
that shape was rejected.

After removing the threaded reset, enabling the policy still constructed a WSGI
deadline wrapper for bodyless GETs. A same-image gate measured -8.5% WSGI RPS;
that shape was also rejected. The accepted path creates blocking or async total
state only for a present body when the option is enabled.

Diagnostic artifacts (gitignored) retain `body-timeout-v1` through
`body-timeout-v3` labels rather than hiding those decisions.

## Performance gates

All final gates use CPython 3.15.0b3 with the GIL, one isolated server CPU,
disjoint client CPUs, balanced order, one-second warmup, five five-second trials,
correct response probes, and zero timed errors.

### Disabled policy versus the preceding frozen image

Artifact: `benchmarks/artifacts/body-timeout-disabled-exact-final-2026-07-11.json`.

| Scenario | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|
| static 1 KiB, 64 connections | +0.22% | 10.82 points | -2.03% | 16.16 points |
| WSGI 1 KiB, 64 connections | -3.93% | 2.82 points | +7.42% | 1.81 points |
| ASGI 1 KiB, 64 connections | -2.99% | 8.35 points | +0.68% | 1.74 points |

All throughput results remain inside the protected 5% budget. Static and ASGI
direction is smaller than dispersion. WSGI p99 is higher in this run, while the
immediately preceding exact-code gate measured -2.6% p99 with 7.9-point MAD;
the evidence does not support a tail-latency claim.

### Enabled versus disabled while consuming 64 KiB

Artifact: `benchmarks/artifacts/body-timeout-body64k-final-2026-07-11.json`.
Both sides use the final `body-timeout-v4` image; only the candidate receives a
30-second body budget.

| Scenario | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|
| WSGI 64 KiB body, 32 connections | -4.38% | 3.53 points | +6.88% | 4.42 points |
| ASGI 64 KiB body, 32 connections | +9.32% | 9.61 points | -8.01% | 1.15 points |

The WSGI wrapper's real body cost is measurable but stays inside budget. ASGI is
classified as neutral rather than a gain because RPS dispersion is wider than
the point estimate. Peak memory differs by at most 0.3 MiB in each pair.

A same-image bodyless gate measured -0.50% ASGI RPS/+0.88% p99. WSGI landed at
-4.86% RPS with noisy +10.95% p99 at 64 connections, then resolved to +2.10%
RPS/-2.91% p99 at concurrency one with dispersion wider than both effects.
Artifacts are `body-timeout-enabled-final-2026-07-11.json` and
`body-timeout-enabled-wsgi-c1-final-2026-07-11.json`.

The final product image is `sha256:9ec15382...`; both exact-source artifacts
record product-tree hash `ea70c44c...` and the final harness/app hashes.

## Decision and remaining work

Accept `request_body_timeout=None` as the default and
`--request-body-timeout SECONDS` as an explicit positive HTTP/1 total budget.
The policy closes the slow-but-progressing body occupancy gap across every
shipped HTTP/1 body consumer without charging bodyless requests outside the
protected budget.

Still open:

- the total production request-head deadline and consistent first-byte/header
  phase semantics are now closed by
  [Total HTTP/1 request-head timeout](2026-07-11-request-head-timeout.md);
- 1 MiB and larger bodies, chunk-count/cardinality, TLS, slow WAN, and application
  processing-between-chunks cohorts;
- timeout/abort counters once bounded-cardinality metrics exist;
- total application runtime, minimum bandwidth/fairness, and HTTP/2/3 stream
  policies; and
- framework-specific cancellation/error behavior.

## Verification

- 808 functional tests pass on CPython 3.15 with the GIL and 808 pass on the
  free-threaded build; both populated environments have four optional skips.
- Deadline, progressive-body, chunked-event, and body-capable loadgen tests pass
  on CPython 3.14.
- Repository-wide Ruff lint/format, Bandit, strict MkDocs, and
  `git diff --check` pass. Ty reports only two pre-existing unused-suppression
  warnings.
- Both final benchmark artifacts' product and four harness/app hashes match the
  current source exactly.
- Fresh wheel and source distributions build, the wheel has no unconditional
  runtime dependencies, and installed import/CLI smoke passes outside the source
  tree.

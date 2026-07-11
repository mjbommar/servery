# Total HTTP/1 request-head timeout — 2026-07-11

Status: accepted as an opt-in HTTP/1 connection-occupancy policy. It is disabled
by default and no profile selects a value.

## Gap and policy

The ordinary threaded parser has a socket progress timeout, but a client can
send one request-line or field byte before each expiry and hold a thread and
admission slot indefinitely. ASGI previously bounded one whole `readuntil()`,
but reused `keepalive_timeout` for that operation after a response. That made a
documented idle-only policy also cap an active next head.

`--request-head-timeout SECONDS` adds one total budget from the first request-
head byte through the terminating blank line. Progress does not reset it. The
three phases are deliberately independent:

1. `--keepalive-timeout` waits for the first byte of a subsequent request (or
   inherits `--timeout` when unset);
2. `--request-head-timeout` bounds the active request line plus fields; and
3. `--timeout` remains the existing active transport limit, with the shorter
   active/total remainder winning.

For the first request, `--timeout` bounds the first-byte wait. Already-buffered
pipelined input satisfies that wait immediately. Expiry closes or aborts the
connection without a late HTTP error: the peer has not delivered a complete
message boundary that can safely be reused.

The setting is optional because legitimate head times vary. Large cookies,
proxy-added fields, slow WAN links, and debugging clients may need more time;
exposed origins may prefer a firm occupancy bound. A positive operator value is
observable policy. Parser chunk sizes, buffered fast paths, and timer mechanisms
remain internal.

## Implementation boundary

The disabled threaded path still calls the inherited stdlib request loop. The
configured loop first peeks without consuming under the idle/progress budget,
then starts an absolute monotonic deadline. A `HeadDeadlineReader` consumes only
through a line boundary, repeatedly caps fragmented waits by the shorter socket
or total remainder, and never steals body or pipelined bytes from the original
`BufferedReader`.

Ordinary heads generally arrive in the first buffered snapshot. A complete-
head fast path finds the terminating blank line, performs one exact read through
it, and parses from `BytesIO`; bytes after that extent remain in the original
stream. Fragmented heads retain the line-by-line deadline path. Socket timeout
transitions occur only when a distinct keep-alive idle value is active or a
fragmented wait needs the shorter remaining total.

ASGI also retains its original one-`readuntil()` loop when neither phase policy
is configured. The configured loop uses one reschedulable `asyncio.timeout`
context: it reads the first byte under the idle budget, reschedules the same
timer for the shorter active/total head budget, and then reads through
`CRLF CRLF`. This also corrects keep-alive-only semantics: first activity ends
the idle phase instead of making the idle value a whole-head deadline.

## Correctness evidence

Threaded and ASGI wire tests use a one-second active timeout and a 150 ms total
head budget. The peer sends fragments every 70 ms, so it continues making
progress well inside the ordinary limit but is disconnected before completing
the blank line. Additional tests prove that:

- a complete buffered head is consumed in one exact read while body bytes stay
  in the original stream;
- two pipelined requests survive configured threaded parsing;
- the first byte of a subsequent request ends a 100 ms keep-alive idle budget,
  allowing the rest of that head to complete under its active/total phase;
- positive-only config and CLI mapping are enforced; and
- the benchmark control reaches only candidate commands.

Existing request-line, field-count/size/syntax, body framing, keep-alive,
pipeline, WSGI, and ASGI suites cover ordinary behavior around the new seam.

## Rejected and optimized shapes

The first correct shape used a deadline-aware line adapter for every field and
two independent ASGI `wait_for()` calls. Its five-trial same-image gate measured
about -10.6% static, -10.8% WSGI, and -8.4% ASGI RPS. It was rejected.

The second shape added the complete-buffer fast path and one reschedulable ASGI
timer. A three-trial diagnostic improved ASGI to a noisy +3.5% point estimate,
but threaded WSGI remained -9.8%. Inspection then found two redundant
`settimeout()` transitions per threaded request when no separate idle value was
configured. Removing those transitions produced -2.9% static and -2.0% WSGI in
the next short gate. The longer final run is retained below rather than replaced
by those more favorable diagnostics.

The rejected first shape's console result was intentionally superseded rather
than mislabeled as final. Diagnostic artifacts retained for the optimization
steps are `head-timeout-v2-enabled-quick.json` and
`head-timeout-v3-enabled-quick.json`; the v3 five-trial artifact below is the
substantive accepted-shape result.

## Performance gates

All final gates use CPython 3.15.0b3 with the GIL, one isolated server CPU,
disjoint client CPUs, correct response probes, balanced order, one-second
warmup, five five-second trials, and zero timed errors.

### Disabled final tree versus the preceding frozen image

Artifact: `benchmarks/artifacts/head-timeout-disabled-final-2026-07-11.json`.

| Scenario | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|
| static 1 KiB, 64 connections | -2.32% | 3.45 points | +3.87% | 6.47 points |
| WSGI 1 KiB, 64 connections | -2.36% | 0.74 points | +3.83% | 1.13 points |
| ASGI 1 KiB, 64 connections | +15.87% | 2.33 points | -13.63% | 0.54 points |

Static and WSGI stay inside the protected 5% throughput budget. The ASGI source
gate reports a large gain, but the disabled code change is only a local timeout-
attribute/branch substitution and supplies no credible mechanism for that size;
it is recorded as a favorable unexplained run, not claimed as a product gain.
Most importantly, no disabled cohort regresses beyond budget.

### Same-image enabled policy cost

Artifacts:

- `benchmarks/artifacts/head-timeout-enabled-final-2026-07-11.json` (64
  connections); and
- `benchmarks/artifacts/head-timeout-enabled-c1-final-2026-07-11.json`
  (concurrency-one resolution).

| Scenario | Connections | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|---:|
| static 1 KiB | 64 | +2.52% | 7.96 points | -7.32% | 3.87 points |
| WSGI 1 KiB | 64 | -13.93% | 5.26 points | +23.60% | 0.98 points |
| ASGI 1 KiB | 64 | -8.20% | 3.01 points | +3.87% | 1.17 points |
| WSGI 1 KiB | 1 | -1.16% | 9.77 points | +0.44% | 5.68 points |
| ASGI 1 KiB | 1 | -6.48% | 5.61 points | +11.82% | 14.04 points |

The setting has a real capacity tradeoff on tiny dynamic responses at high
concurrency: observing the first-byte boundary requires an extra buffered-stream
phase in the threaded adapter and an extra stream await in ASGI. Concurrency one
is smaller/noisier, especially for WSGI, so scheduling amplifies the 64-client
cost. Static work makes the fixed parser cost insignificant. This is acceptable
for an explicit abuse-control policy, but is the reason it remains disabled and
absent from profiles rather than becoming an unconditional parser change.

The three substantive gates use image `sha256:c0c47aeb...` and record product-
tree hash `d1d8345f...` plus identical harness/application hashes. A final type-
only cleanup replaced dynamic protocol-hook suppressions with `typing.cast` and
corrected the temporary `rfile` annotation; it does not change executed policy
logic. The exact current image is `sha256:bde8550f...`, product-tree hash
`9c606dd3...`.

`head-timeout-typing-final-control-2026-07-11.json` compares that exact image to
the substantive v3 image for three trials. Paired RPS is +4.44% static, -4.74%
WSGI, and -3.67% ASGI; all are inside the 5% control boundary and ASGI/WSGI
dispersion is wider than the point estimates. This short control establishes
that the annotation cleanup did not overturn the longer decision evidence; it
does not replace the five-trial gates above.

## Decision and follow-ups

Accept `request_head_timeout=None` and
`--request-head-timeout SECONDS` as a positive, opt-in HTTP/1 policy. It closes
the production slow-but-progressing head gap and makes keep-alive idle semantics
phase-correct across threaded and ASGI adapters without charging the default
per-request path.

Still open:

- large-cookie/proxy headers, TLS, high fragmentation, slow WAN, and churn
  cohorts;
- bounded-cardinality timeout/abort counters;
- HTTP/2/3 per-stream header-block phase policy;
- minimum bandwidth/fairness and total application-runtime policy; and
- whether a future owned async connection parser can observe first-byte arrival
  with less configured overhead than public `StreamReader` permits.

## Verification

- 816 functional tests pass on CPython 3.15 with the GIL and 816 pass on its
  free-threaded build; both populated environments have four optional skips.
- Repository-wide Ruff lint/format, `git diff --check`, Bandit, strict MkDocs,
  and ty all pass.
- Slow-progress, keep-alive phase, exact buffered-head, pipeline, config/CLI,
  ASGI, and benchmark-control tests pass within the full suites.
- The focused deadline/config/harness suites pass on CPython 3.14. Fresh wheel
  and source distributions build; the wheel has no unconditional runtime
  dependencies and passes import/CLI smoke outside the source tree.

# Shared HTTP/1 request parser — 2026-07-10

Status: shared request-head parsing and policy primitives accepted; complete
selector connection state machine not accepted. Results are machine-specific
engineering evidence, not portable performance claims.

## Hypothesis

The threaded HTTP/1 handler and a future selector frontend can share request-line
and header semantics without materially slowing the existing hot path. Extracting
these rules first should reduce the chance that a selector becomes a second,
drifting HTTP implementation.

## Implemented slice

`servery._request` now owns:

- request-line decoding, version validation, HTTP/0.9 handling, connection-close
  defaults, and the leading-`//` authority-collapse rule;
- precise error state needed to preserve the threaded handler's response-version
  timing on malformed requests;
- the case-insensitive, first-value-wins request-header view;
- a specialized blocking header reader for the threaded handler; and
- a bounded incremental header-block parser that accepts arbitrary fragments and
  returns bytes after the terminating blank line.

The benchmark-only selector spike consumes the shared request-line and header
parsers. `RequestHeadParser` composes them with the shared anti-smuggling framing
rules and returns body framing, connection-persistence, and
`Expect: 100-continue` policy. The production handler uses the same finalizer
through its specialized buffered adapter. The spike still owns an incomplete
connection loop and is not a production backend. Request-body consumption,
multi-request/pipelining disposition, timeouts, admission, and write backpressure
have not moved into a common connection state machine.

## Correctness contract

The extraction initially preserved existing behavior. The subsequent conformance
audit deliberately tightened two security-sensitive gaps already required by
`FR-HOST-01` and RFC 9112: malformed field syntax and missing, duplicate, or
invalid HTTP/1.1 `Host` now return `400` and close. These are wire-safety
invariants, not configurable compatibility choices. Tests cover:

- HTTP/1.0, HTTP/1.1, generic valid HTTP/1.x, unsupported HTTP/2 request lines,
  HTTP/0.9 GET, malformed versions, syntax errors, and leading `//` paths;
- response-version and close-policy timing for errors;
- case-insensitive names, duplicate lookup, invalid field names/values, obs-fold,
  EOF without a blank line, line/count limits, and post-completion misuse;
- every byte split in a representative header block, including leftover body
  bytes; and
- every byte split across a complete request head, including framing, `Expect`,
  persistence, EOF, body-size/chunked policy, and post-head bytes; and
- end-to-end abuse-limit, HTTP/1 conformance, and ordinary server behavior.

Static analysis and 110 focused request/body/security/conformance/server tests passed.
A containerized selector smoke served the validated 1 KiB workload with zero
errors after adopting the shared parser.

## Hot-path tradeoff

The first implementation used a frozen slots dataclass for parsed request facts
and routed every blocking header through the incremental parser's line helper.
That was conceptually tidy but made Python call/object cost visible on the tiny
response path. A direct CPython 3.15 microbenchmark measured request-line parsing
at about 0.58 microseconds versus 0.21 microseconds for the old inline fast path.

The accepted implementation uses a normal slots dataclass and retains a
specialized blocking header loop in the shared module. The request-line cost fell
to about 0.35 microseconds. This deliberately duplicates a small line-processing
loop inside one module: the blocking and incremental adapters share limits,
types, and tested semantics, while avoiding a Python function call per header on
the production threaded path. There is no new public configuration because this
is an internal representation choice, not an operator resource policy.

## Paired external evidence

The candidate image was compared with a preserved image from the same shared
static-response source, built immediately before request-parser extraction.
Candidate and baseline rotated within each trial on one isolated server CPU; all
responses passed status, length, and hash probes, timed errors were zero, and the
client remained below saturation.

The initial seven-trial implementation was mixed: keep-alive throughput improved
3.0%, but paired median p99 rose 7.1%. After removing frozen-object and per-header
call overhead, a second seven-trial run was statistically noisy: −0.3% throughput
and +5.9% p99, with 6.0- and 8.7-point MAD respectively. Its aggregate candidate
p99 was actually lower than the baseline, so that result was not credible as a
regression decision.

The disputed keep-alive cohort was therefore repeated for 11 longer, five-second
trials:

| Workload | Median paired RPS change | RPS-change MAD | Median paired p99 change | p99-change MAD |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns | +2.9% | 2.6 points | −2.7% | 6.7 points |
| 1 KiB churn, 32 conns (7 trials) | −0.3% | 10.1 points | −2.0% | 1.7 points |

Local ignored artifacts under `benchmarks/artifacts/`:

- `request-plan-paired-2026-07-10.json` (first implementation);
- `request-plan-optimized-paired-2026-07-10.json`; and
- `request-plan-optimized-keepalive-long-2026-07-10.json`.

The point estimates do not justify a performance claim, but they rule out a
repeatable regression near the protected 5% budget. The value of this change is
semantic convergence, with end-to-end performance effectively neutral.

### Incremental request-head follow-up

The next slice added the complete incremental request-head state and moved the
threaded handler's framing, persistence, and `Expect` decisions through the same
pure finalizer. A seven-trial paired run isolated this slice from the already
accepted parser image:

| Workload | Median paired RPS change | RPS-change MAD | Median paired p99 change | p99-change MAD |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns | +1.0% | 5.3 points | −4.7% | 13.2 points |
| 1 KiB churn, 32 conns | −1.1% | 2.5 points | +1.5% | 1.4 points |

The changes are inside the noise/protected budget, client CPU was 24–36%, and
timed errors were zero. The selector then passed a fresh validated static smoke.
Artifact: ignored local file `request-head-paired-2026-07-10.json` under
`benchmarks/artifacts/`.

### Host and field-syntax conformance follow-up

The shared boundary exposed an older documented-but-unimplemented requirement:
`FR-HOST-01` promised `400` for missing, duplicate, or invalid HTTP/1.1 `Host`,
while the permissive parser also accepted whitespace before a colon, colonless
lines, invalid field-name bytes, and control bytes in values. Both adapters now
reject these forms with `400` and force connection close; header size/count
limits remain the semantically distinct `431` path.

The first strict implementation checked every byte in Python. It was rejected as
an implementation strategy after a seven-trial paired run measured −12.5%
throughput / +14.6% p99 for keep-alive and −4.1% / +3.9% for churn. Compiled byte
regular expressions replaced the Python loops without weakening the grammar.
The optimized result versus the pre-validation request-head image was:

| Workload | Median paired RPS change | RPS-change MAD | Median paired p99 change | p99-change MAD |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns (11 × 5 s) | −3.4% | 4.9 points | +3.0% | 16.4 points |
| 1 KiB churn, 32 conns (7 × 3 s) | −2.6% | 3.3 points | +3.0% | 1.8 points |

The accepted implementation has a small measurable throughput cost, but remains
inside the 5% protected budget, completed with zero errors and ample client
headroom, and closes mandatory wire-safety requirements. Permissive parsing is
not offered as configuration: an operator resource tradeoff cannot make request
desynchronization safe.

Additional ignored local artifacts under `benchmarks/artifacts/`:

- `request-host-paired-2026-07-10.json` (rejected Python byte loops);
- `request-host-optimized-keepalive-2026-07-10.json` (short noisy run);
- `request-host-optimized-keepalive-long-2026-07-10.json`; and
- `request-host-optimized-churn-2026-07-10.json`.

## Decision and next gate

Accept the shared request-head types, incremental parser, and fast buffered
adapter, including strict `Host` and field syntax. Keep the threaded backend as
the only production HTTP/1 frontend and expose no backend setting.

Next, define the multi-request connection layer: body-byte consumption/draining,
leftover and pipelined-head ownership, maximum requests per connection, read/idle
deadlines, and precise error serialization. Only after both the buffered and
selector adapters pass the same slow-input, smuggling, timeout, pipelining, and
conformance corpus should Stage D be considered complete.

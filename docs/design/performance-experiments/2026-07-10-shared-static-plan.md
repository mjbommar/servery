# Shared static representation plan — 2026-07-10

Status: accepted architecture slice. The larger transport-neutral response plan
and production selector remain in progress.

## Hypothesis

HTTP/1, HTTP/2, and HTTP/3 can share acquisition and immutable representation
facts without materially slowing the HTTP/1 hot path. One object should own the
opened file, `fstat` result, content type, coding, ETag, and Last-Modified value.
This removes duplicate decisions and gives a future selector a tested boundary.

## Implementation

`servery._static.open_file` opens the file once and returns a slots-based
`FileBody`. HTTP/1 consumes the facts while retaining its complete range,
conditional, download, compression, and sender behavior. HTTP/2 and HTTP/3
transfer the same object to their flow-controlled body emitters and close it on
HEAD, success, reset, cancellation, error, or connection teardown.

There is no public configuration change. Existing policy remains where it
belongs:

- `small_file_buffer_size` controls HTTP/1 bounded read versus sendfile;
- `max_buffered_response` controls when HTTP/2/3 transfer an open file instead of
  retaining bytes;
- compression thresholds and caches retain their existing configuration.

## Differential correctness contract

`tests/test_static_response_contract.py` now compares HTTP/1 wire results with
the shared HTTP/2/3 planner for the overlapping contract:

- regular and empty GET/HEAD bodies;
- MIME, content length, ETag, Last-Modified, cache, CORS, and security headers;
- conditional `304` status and validators;
- gzip coding, representation ETag, `Vary`, and decoded bytes;
- directory redirects;
- missing and escaped paths.

Ranges, `If-Range`, index lookup, SPA fallback, `?download`, archives, and styled
errors remain explicit HTTP/1 capability differences rather than false parity.
Separate existing tests continue to cover those features.

Replacement-race tests prove an atomically replaced path cannot make the planned
content length describe the old file while HTTP/2/3 stream the new one.
`ResourceWarning`-as-error runs cover large GET/HEAD ownership, flow control, and
HTTP/2/3 helper paths.

## Performance evidence

### Stage-level cost

On CPython 3.15, a direct open/fstat/coding/validator/close loop took a median
2.63 microseconds. The optimized shared object took 2.86 microseconds: about
0.23 microseconds of object cost per file response. Removing unnecessary frozen
dataclass semantics reduced the initial approximately 0.51 microsecond overhead.

### Why standalone medians were insufficient

Two standalone five-trial runs moved downward with the host over time. Comparing
their aggregate medians to an earlier artifact suggested regressions ranging from
3–9%, inconsistent with the microbenchmark and with each other. They are retained
as diagnostic artifacts, not causal evidence:

- `shared-static-plan-protected-2026-07-10.json`;
- `shared-static-plan-optimized-2026-07-10.json`.

The harness now accepts a separately built baseline image, rotates baseline and
candidate within every trial, and reports the median of per-trial ratios. This
avoids the invalid ratio-of-independent-medians comparison.

### Seven-trial paired result

The baseline image used the same source and 16 KiB optimization but retained the
pre-extraction inline HTTP/1 code. Both image identities are in the artifact.

| Workload | Median paired RPS change | RPS-change MAD | Median paired p99 change | p99-change MAD |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive, 64 conns | +1.9% | 4.0 points | +2.3% | 2.7 points |
| 1 KiB churn, 32 conns | −0.03% | 5.8 points | +1.3% | 0.8 points |

All responses were correct, client CPU remained below saturation, and errors
were zero. Individual RPS ratios ranged from −10.7% to +5.9% for keep-alive and
−15.9% to +5.8% for churn, confirming substantial host/order noise; neither
cohort shows a consistent regression near the roadmap's 5% protected budget.

Artifact: ignored local file `shared-static-plan-paired-2026-07-10.json` under
`benchmarks/artifacts/`.

## Decision

Accept the shared acquisition/representation layer. It removes duplicate hot-path
logic, fixes HTTP/2/3 identity ownership, and creates the first transport-neutral
boundary with paired performance effectively neutral. Keep HTTP/1 response
semantics and body emission unchanged for now.

Next, extend the differential corpus before moving range or directory decisions.
Do not make the selector production-visible until the incremental parser and
response plan pass the complete HTTP/1 conformance corpus.

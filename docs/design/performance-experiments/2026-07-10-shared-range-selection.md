# Shared conditional and range selection — 2026-07-10

Status: accept the shared identity-selection primitive and the optimized HTTP/1
adapter shape. Accept conditionals and single ranges in the benchmark-only
selector prototype. Do not change the production buffer default or expose a
selector backend from this result.

## Hypothesis and boundary

The production handler and any future connection backend must not independently
decide whether an opened identity produces `200`, `206`, `304`, or `416`.
Duplicating that logic risks different validator precedence, `If-Range`
semantics, offsets, or counts. The narrow experiment extracted only that
decision; it did not attempt to share wire-header construction, body emission,
directories, compression, or transport flow control.

`_static.select_identity` consumes immutable facts from the already-opened file
and the relevant request fields. It returns a status, byte offset, exact count,
and optional `Content-Range`. `_conditional.if_range_matches` owns strong
entity-tag and HTTP-date matching. Both the threaded handler and selector
prototype retain a no-allocation common path when no Range or conditional field
is present.

This separation keeps three kinds of choice distinct:

- HTTP conditional and range precedence is protocol correctness and is not
  configurable.
- The body-transfer mechanism remains transport-owned.
- The memory/throughput crossover remains the existing configurable
  `small_file_buffer_size`; `0` forces sendfile/streaming and a positive value
  permits bounded reads through that size.

## Correctness and ownership gates

The shared contract covers a full response, bounded and suffix ranges,
unsatisfiable ranges, `If-Range` mismatch fallback, and conditional `304`.
Selector integration additionally covers GET/HEAD wire behavior, including
`206`, `304`, and `416`.

Two identity-race tests protect the resource boundary:

- atomic replacement after open still sends the original opened identity and
  validators;
- truncation after open cannot silently claim success. The sender stops at the
  bytes actually available, aborts the connection, and increments a bounded
  transfer-error counter rather than reparsing or reopening the path.

The comparison client now supports validated custom request fields and recognizes
RFC-bodyless 1xx/204/304 responses without requiring `Content-Length`. The new
opt-in scenarios probe a 1 KiB range from a 64 KiB file and an
`If-Modified-Since` `304`; status, length, and body hash are checked before every
timed sample.

## Production refactor gate

The first adapter shape constructed a selection object for every request. It was
rejected after a paired run showed a 5.9% churn throughput regression, outside
the protected 5% budget. The accepted shape checks whether any relevant request
field exists before entering shared selection.

CPython 3.15.0b3 with the GIL enabled, one server CPU, two isolated client
processes, 64 connections, warm cache, seven rotated three-second trials, and
zero timed errors:

| Header-heavy workload | Current vs pre-refactor RPS | RPS ratio MAD | Current vs pre-refactor p99 | p99 ratio MAD |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB range from 64 KiB | -4.3% | 4.8% | +4.5% | 20.6% |
| bodyless `304` | -4.2% | 3.4% | -1.3% | 16.5% |

The result is inside the gate but near its edge. It is accepted for semantic
convergence and reduced duplicate logic, not as a speed improvement. Tail ratios
are too dispersed for a directional claim. The ordinary no-Range/no-conditional
gate was separately neutral after the fast-path revision: static 1 KiB was
-2.5% RPS and churn -0.2%, with dispersion wider than the effects.

## Selector semantic result

The same host/runtime controls and seven rotated trials compared production with
the selector's 16 KiB default and its explicit 64 KiB buffer control:

| Workload | Server/policy | Median RPS | Median p99 | Peak memory |
| --- | --- | ---: | ---: | ---: |
| 1 KiB range | production | 14.16k | 21.52 ms | 34.1 MiB |
| 1 KiB range | selector, sendfile above 16 KiB | 11.32k | 6.89 ms | 27.8 MiB |
| 1 KiB range | selector, buffer through 64 KiB | 14.00k | 5.45 ms | 25.8 MiB |
| `304` | production | 17.85k | 15.68 ms | 29.7 MiB |
| `304` | selector, 16 KiB policy | 16.96k | 4.75 ms | 27.5 MiB |
| `304` | selector, 64 KiB control | 16.16k | 4.80 ms | 25.8 MiB |

For ranges, the default selector is 20.1% below production throughput but 68.0%
lower at p99. Buffering through 64 KiB changes that to throughput parity (-1.1%)
and 74.7% lower p99, reinforcing the previously measured asyncio-sendfile
crossover. For `304`, no body is transferred: production remains 5.0–9.5%
faster, while selector p99 is about 70% lower. This isolates connection/runtime
overhead from file transfer and shows that throughput and tail latency remain
different decisions.

The 64 KiB result is still not permission to change the production default.
Its transient body budget scales with active sends, and TLS, other operating
systems, slow consumers, and high-cardinality files remain untested. Operators
already have the right policy control; a future selector profile may choose a
different value only after its own memory and platform gates.

Ignored raw artifacts under `benchmarks/artifacts/`:

- `shared-range-selection-paired-2026-07-10.json` (rejected first shape);
- `shared-range-selection-v2-paired-2026-07-10.json` (ordinary optimized path);
- `shared-range-selection-semantics-paired-2026-07-10.json` (header-heavy paired gate);
- `shared-range-selector-semantics-2026-07-10.json` (selector comparison).

## Decision and next gate

Keep the shared primitive small and transport-neutral. Do not move response
header serialization or body mechanics into it. The next selector conformance
work should add directories/index redirects and representation-policy parity
before compression or digest work; each slice must continue to use the opened
identity and prove cancellation/close ownership.

Before any public selector setting, repeat the range/conditional corpus on macOS
and Windows, under TLS fallback, with normal-GIL and free-threaded interpreters,
and under slow-reader/abort conditions. A public backend still requires an
explicit feature-routing decision rather than silently returning reduced static
semantics.

## Verification

- 751 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds; each run has four expected optional-integration skips.
- Repository-wide Ruff format/lint, ty, Bandit, strict MkDocs, and
  `git diff --check` gates pass.
- The wheel and source distribution build; the wheel declares zero runtime
  dependencies and imports/runs `servery --version` from a clean directory.

# Native Uvicorn and ASGI concurrency scaling — 2026-07-11

Status: accepted as a benchmark-harness and capability result. No native
dependency enters servery's package, runtime, or default comparison image.

## Questions

This slice asks three separate questions:

1. How does servery compare with Uvicorn's portable `asyncio`/h11 path when all
   three servers use the same CPython 3.14 image?
2. How much do Uvicorn's optional `uvloop`/`httptools` implementations change
   that comparison?
3. Can the current loopback client honestly measure 100, 1,000, and 10,000
   concurrent nonblocking waits?

Keeping these questions separate matters. Uvicorn's `auto` mode silently uses
native packages when they are installed. The harness therefore pins the
portable adapter to `--loop asyncio --http h11` and the native adapter to
`--loop uvloop --http httptools`. An early diagnostic that did not make this
explicit was discarded and rerun.

## Runtime and dependency boundary

The opt-in image pins:

- CPython 3.14.3 with the GIL;
- Uvicorn 0.51.0;
- uvloop 0.22.1; and
- httptools 0.8.0.

Current uvloop and httptools releases publish CPython 3.14 wheels but not
CPython 3.15 wheels. `--include-uvicorn-native` therefore installs them only in
a deliberately labeled compatible image. Selecting `uvicorn-native` without
that flag fails early, and missing native modules also fail before timing.

This is competitor configuration, not a proposal to weaken servery's
zero-dependency contract. Adding a compiler or native runtime packages to the
standard Python 3.15 image would hide the ecosystem boundary and change every
other comparison.

## Load-generator controls

High-concurrency work exposed three client validity problems. The load
generator now offers explicit choices rather than silently changing all runs:

- `--max-latency-samples N` retains a deterministic stratified reservoir of at
  most `N` observations. Request, byte, error, and throughput counts remain
  exact; latency quantiles and the mean become sample estimates. Omitting the
  option preserves the historical all-observations behavior.
- `--persistent-warmup` keeps connections open across the untimed/timed
  boundary. The default separate-run warmup remains useful for reconnect-burst
  tests.
- `--connection-ramp SECONDS` staggers connection starts within each client
  process. Zero preserves simultaneous launch.

Initial connects are bounded by the remaining run deadline. Results split
unexpected HTTP statuses from transport/connect failures and record the exact
status histogram, so overload cannot be misread as application throughput.

## Fair matrix

The final valid cohorts use the same image, application fixture, one server
CPU, four isolated client processes, plaintext HTTP/1.1, exact preflight body
validation, one-second separate warmup, deterministic server-order rotation,
and five five-second trials. Timed errors are zero. At most 100,000 latency
observations are retained per timed sample; request counts and RPS are exact.

Artifacts:

- `benchmarks/artifacts/uvicorn-native-py314-asgi1k-final-v2-2026-07-11.json`;
- `benchmarks/artifacts/uvicorn-native-py314-wait-final-v2-2026-07-11.json`; and
- the `c10000` smoke/diagnostic artifacts for rejected overload evidence.

The exact image is
`sha256:9ce72d1676b60f4477c13cf629511f9a884aae056f0609993d98584b76315850`.
Both accepted artifacts record product-tree hash `28c8123e...` and the exact
harness, client, Dockerfile, and fixture hashes.

### Absolute medians

| Workload | Connections | Server | Median RPS | RPS MAD | Median p99 | Peak MiB | Errors |
|---|---:|---|---:|---:|---:|---:|---:|
| 1 KiB immediate ASGI | 64 | servery | 29,419 | 2.9% | 2.84 ms | 25.7 | 0 |
| 1 KiB immediate ASGI | 64 | Uvicorn portable | 12,340 | 1.6% | 6.18 ms | 24.0 | 0 |
| 1 KiB immediate ASGI | 64 | Uvicorn native | 64,471 | 0.2% | 1.71 ms | 24.4 | 0 |
| 10 ms nonblocking wait | 100 | servery | 7,700 | 3.5% | 14.20 ms | 26.2 | 0 |
| 10 ms nonblocking wait | 100 | Uvicorn portable | 6,774 | 7.3% | 15.74 ms | 24.4 | 0 |
| 10 ms nonblocking wait | 100 | Uvicorn native | 8,787 | 0.3% | 12.63 ms | 24.9 | 0 |
| 10 ms nonblocking wait | 1,000 | servery | 29,885 | 0.7% | 39.22 ms | 39.3 | 0 |
| 10 ms nonblocking wait | 1,000 | Uvicorn portable | 11,212 | 0.9% | 95.85 ms | 38.5 | 0 |
| 10 ms nonblocking wait | 1,000 | Uvicorn native | 45,393 | 0.3% | 30.59 ms | 36.8 | 0 |

### Paired interpretation

Median within-trial servery change is:

| Workload | Connections | Comparison | RPS change | RPS ratio MAD | p99 change | p99 ratio MAD |
|---|---:|---|---:|---:|---:|---:|
| Immediate 1 KiB | 64 | portable Uvicorn | +134.6% | 7.4 points | -54.3% | 0.5 points |
| Immediate 1 KiB | 64 | native Uvicorn | -54.8% | 1.9 points | +66.2% | 2.3 points |
| Wait 10 ms | 100 | portable Uvicorn | +10.1% | 5.6 points | -9.5% | 6.0 points |
| Wait 10 ms | 100 | native Uvicorn | -12.6% | 3.6 points | +12.4% | 1.9 points |
| Wait 10 ms | 1,000 | portable Uvicorn | +168.5% | 4.1 points | -58.8% | 0.5 points |
| Wait 10 ms | 1,000 | native Uvicorn | -33.9% | 0.5 points | +23.9% | 1.0 points |

Within the Uvicorn implementation, native versus portable median RPS changes
are +420.9% for immediate 1 KiB, +30.7% for the 100-client wait, and +303.1%
for the 1,000-client wait. The acceleration benefit is workload-dependent, not
a constant multiplier.

## Why 10,000 is not ranked

Every 10,000-client shape was invalid for ranking on this host. The best
steady-state attempt used a 12-second persistent warmup, a 10-second ramp, and
a three-second timed interval. It still recorded 14,880 servery and 111,619
native-Uvicorn transport errors; the native-Uvicorn client tier reached 96%
CPU. Every completed response had status 200, so this is connection/client
capacity failure rather than application correctness.

The host has a 28,232-port IPv4 ephemeral range and 4,096 SYN/backlog ceilings.
Repeated loopback failures also accumulate `TIME_WAIT` sockets. A 10,000-client
claim needs a dedicated load-generator host or source-IP/port sharding, live
established-connection accounting, controlled kernel settings, and an error-free
steady interval. Increasing warmup again on this host would tune around the
testbed rather than establish server capacity.

The simultaneous-launch artifacts remain useful admission-overload evidence,
but their RPS and p99 values must not rank servers.

## Decision and roadmap impact

- Keep portable and native Uvicorn as distinct explicit adapters. Never use
  `auto` for a labeled comparison.
- Keep latency retention, warmup shape, and connection ramp configurable. The
  default remains compatible with earlier artifacts.
- Treat 100 and 1,000 nonblocking clients as closed for this loopback tier.
  Treat 10,000 as an infrastructure gate, not a servery performance result.
- Do not add uvloop/httptools to servery. Native Uvicorn establishes a real
  performance ceiling, especially for tiny protocol-bound requests, but it
  trades away servery's portability and zero-dependency premise.
- Research stdlib-compatible parser/write-path reductions, multi-process
  scaling, and dedicated-host connection tests. Do not infer that one native
  component or one event-loop rewrite alone explains the entire gap.
- Preserve framework, streaming, cancellation, overload, and memory gates;
  tiny-response RPS is only one capability axis.

Follow-up framework evidence is now recorded in
[ASGI framework compatibility and comparison](2026-07-11-asgi-framework-compatibility.md):
all pinned Starlette/FastAPI probes pass under the explicit native adapter, and
the five-trial native framework tier is materially faster than servery without
changing the zero-dependency decision.

## Verification

- 36 focused comparison/load-generator tests pass on CPython 3.15.
- Explicit adapter tests prove portable `asyncio`/h11 and native
  `uvloop`/`httptools` commands, including the 20,000 admission setting used by
  the 10,000-client probe.
- Ruff and formatting checks pass for the modified harness and tests.
- All accepted timed samples pass exact preflight validation and contain zero
  status or transport errors.

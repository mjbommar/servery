# ASGI framework compatibility and comparison — 2026-07-11

Status: accepted as a benchmark-only compatibility and realism gate. No
third-party framework enters servery's package or runtime dependency set.

## Why this cohort exists

The minimal ASGI callable is useful for isolating server overhead, but it does
not establish that framework task groups, request wrappers, validation, JSON
serialization, and lifespan behavior work. The roadmap therefore calls for the
same representative framework application to run under servery and Uvicorn from
one image.

The pinned inputs reflect current releases on the measurement date:

- [Starlette 1.3.1](https://www.starlette.io/release-notes/), released June 12,
  2026;
- [FastAPI 0.139.0](https://fastapi.tiangolo.com/release-notes/), released July
  1, 2026;
- Pydantic 2.13.4 as resolved by that FastAPI pin; and
- portable Uvicorn 0.51.0 with asyncio/h11, without silently enabling
  `uvloop`/`httptools`.

The same availability rule applies to native Uvicorn acceleration. Current
[uvloop 0.22.1](https://pypi.org/project/uvloop/) and
[httptools 0.8.0](https://pypi.org/project/httptools/) publish CPython 3.14
wheels, including free-threaded variants, but no cp315 wheels. Uvicorn documents
`uvloop` and `httptools` as faster optional implementations. A fair native
cohort is therefore feasible today on the labeled 3.14 image, not the standard
3.15 comparison image.

## Runtime split and configuration

Starlette and FastAPI cannot honestly be collapsed into one current CPython 3.15
image. Starlette 1.3.1 is pure Python and runs on the pinned 3.15.0b3 image.
FastAPI resolves Pydantic Core 2.46.4, which currently publishes a CPython 3.14
wheel but no CPython 3.15 wheel. On the standard 3.15 slim image, pip falls back
to a Rust build and fails because the intentionally lean runtime has no C
linker.

The harness makes that boundary explicit:

- the standard comparison image pins Starlette 1.3.1;
- `--include-fastapi` adds FastAPI 0.139.0 only when deliberately requested;
- the current FastAPI command uses `--python-image python:3.14.3-slim`; and
- a FastAPI scenario fails early with a clear message if the selected image
  does not contain FastAPI.

Adding a compiler/Rust toolchain to the normal 3.15 image would hide ecosystem
readiness and change the reproducibility/security surface of every comparison.
The right future action is to rerun when Pydantic Core publishes a CPython 3.15
wheel. This is benchmark configuration, not a servery feature flag.

## Workloads and fairness

`benchmarks/comparison/starlette_apps.py` and `fastapi_apps.py` are separate,
read-only mounted fixtures. The scenario records the exact app spec, expected
status, length, and body hash. Only servery ASGI and Uvicorn participate.

- `asgi-starlette-json` exercises `Request.url`, query parameters, routing, and
  `JSONResponse`.
- `asgi-starlette-stream-64k` emits 16 4 KiB events through
  `StreamingResponse`, including its concurrent disconnect listener.
- `asgi-fastapi-json` exercises integer path/query validation and JSON
  serialization.
- `asgi-fastapi-validation` requires the exact `422` Pydantic validation body;
  it is a compatibility smoke rather than a throughput claim.

Both servers use the same image, interpreter, app file, one CPU, one worker,
plaintext HTTP/1.1, warmup, load generator, and balanced order. The harness now
reports within-trial paired ratios for servery-versus-Uvicorn as well as
candidate-versus-frozen-servery comparisons.

## Compatibility result

Every preflight and timed request returned the expected status, exact length,
and exact body hash with zero errors:

- Starlette JSON and 16-event `StreamingResponse` pass on CPython 3.15.0b3;
- Starlette's producer/disconnect task group completes at one and 32
  connections without hanging or consuming pipelined request bytes;
- FastAPI/Pydantic success responses pass on CPython 3.14.3; and
- servery, portable Uvicorn, and native Uvicorn return the exact pinned `422`
  validation response in the combined Python 3.14 image.

This closes the known basic Starlette/FastAPI compatibility gap. It does not
claim exhaustive framework conformance: multipart/form parsing, background
tasks, SSE, exception middleware, WebSockets, lifespan failure, and long-lived
leak tests remain open.

## Performance evidence

Artifacts:

- `benchmarks/artifacts/framework-starlette-final-2026-07-11.json`;
- `benchmarks/artifacts/framework-fastapi-py314-final-2026-07-11.json`;
- `benchmarks/artifacts/framework-native-py314-final-2026-07-11.json`;
- `benchmarks/artifacts/framework-native-py314-compatibility-smoke-2026-07-11.json`;
- `framework-fastapi-validation-smoke.json`; and
- the initial one-trial Starlette/FastAPI smoke artifacts.

The final matrices use five five-second trials after one-second workload
warmups. Results below are median within-trial servery change versus Uvicorn.

| Runtime/workload | Connections | RPS change | RPS ratio MAD | p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|---:|
| 3.15 Starlette JSON | 1 | +74.21% | 5.12 points | -37.42% | 1.69 points |
| 3.15 Starlette JSON | 64 | +80.01% | 19.13 points | -37.29% | 4.27 points |
| 3.15 Starlette stream 64 KiB | 1 | +72.41% | 6.49 points | -39.89% | 5.91 points |
| 3.15 Starlette stream 64 KiB | 32 | +95.73% | 9.80 points | -45.88% | 0.95 points |
| 3.14 FastAPI JSON | 1 | +62.60% | 4.86 points | -31.29% | 3.25 points |
| 3.14 FastAPI JSON | 64 | +34.66% | 6.13 points | -25.63% | 3.48 points |

These are server/runtime comparisons for small in-process applications, not a
claim about database-backed production APIs. Starlette's 64-connection JSON RPS
dispersion is material, but even its minimum within-trial change remains
positive. Peak cgroup memory is essentially equal within each image: roughly
28 MiB for Starlette and 40 MiB for FastAPI.

Exact images are `sha256:b51ae356...` for Starlette/CPython 3.15 and
`sha256:5e9039ca...` for FastAPI/CPython 3.14. Both contain product-tree hash
`28c8123e...`.

### Labeled native framework cohort

A second Python 3.14.3 image installs FastAPI and the native Uvicorn options
together. Portable Uvicorn is still forced to asyncio/h11; native Uvicorn is
forced to uvloop/httptools. This prevents installed optional packages from
silently changing the portable adapter.

The final matrix uses five five-second trials, one-second warmups, deterministic
order rotation, four client processes, one server CPU, exact response probes,
and zero timed errors. Results are median within-trial servery changes.

| Workload | Connections | Comparison | RPS change | RPS ratio MAD | p99 change | p99 ratio MAD |
|---|---:|---|---:|---:|---:|---:|
| Starlette JSON | 64 | portable Uvicorn | +60.3% | 4.4 points | -35.1% | 1.5 points |
| Starlette JSON | 64 | native Uvicorn | -49.9% | 1.1 points | +66.3% | 1.3 points |
| Starlette stream 64 KiB | 32 | portable Uvicorn | +100.4% | 5.4 points | -45.9% | 1.2 points |
| Starlette stream 64 KiB | 32 | native Uvicorn | -26.2% | 3.4 points | +27.1% | 1.6 points |
| FastAPI JSON | 64 | portable Uvicorn | +52.6% | 6.9 points | -31.7% | 2.5 points |
| FastAPI JSON | 64 | native Uvicorn | -32.9% | 1.6 points | +27.9% | 3.7 points |

Native Uvicorn versus portable Uvicorn improves median RPS by 227.6% for
Starlette JSON, 171.4% for Starlette streaming, and 118.7% for FastAPI JSON in
the same trials. This is a real native/runtime ceiling. It does not isolate how
much belongs to the event loop versus the HTTP parser, and it does not justify
adding either dependency to servery.

The exact combined image is
`sha256:d2d79e7bc4bc114e2a24e70a4cf58b4b3ae71111f7a99a6f9b4ebc4a6ba57f40`.
It contains Python 3.14.3, Starlette 1.3.1, FastAPI 0.139.0, Pydantic 2.13.4,
Uvicorn 0.51.0, uvloop 0.22.1, and httptools 0.8.0, with product-tree hash
`28c8123e...`.

## Decision and follow-ups

Retain the pinned, opt-in framework scenarios and external paired summaries.
They show that servery's ASGI lifecycle is credible under real framework code
and that portable Uvicorn is not currently ahead on these narrow workloads.
Native Uvicorn is materially ahead, establishing the cost of servery's
pure-Python/stdlib portability boundary on protocol-heavy framework traffic.
They do not close the operational gap: Uvicorn still has a broader deployment
ecosystem, native acceleration options, process management integrations, and
far more production exposure.

Next gates:

- rerun FastAPI on CPython 3.15 when a Pydantic Core wheel exists;
- rerun the labeled native framework cohort when CPython 3.15 wheels exist;
- exercise background tasks, exception middleware, SSE, multipart limits, and
  lifespan failure/shutdown;
- add framework WebSocket echo/fanout and long-lived connection gates; and
- run longer recycling/leak tests rather than inferring stability from RPS.

## Verification

- Comparison scenario/adapter/provenance tests pass on GIL and free-threaded
  CPython 3.15 (31 tests including load-generator coverage).
- End-to-end Docker preflights and every timed framework sample pass exact
  status, length, and body-hash checks with zero errors.
- Repository-wide Ruff, ty, `git diff --check`, and strict MkDocs pass after the
  framework harness/docs changes.

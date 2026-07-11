# ASGI streaming response backpressure — 2026-07-10

Status: accepted for intermediate ASGI response-body events, with slow-reader
memory and cancellation evidence. A distinct write-progress timeout remains
open.

## Problem and decision

`_ResponseState.send()` previously queued every ASGI body event with
`StreamWriter.write()` and drained only after the application coroutine returned.
A long-running generator could therefore produce data faster than a slow client
consumed it and grow the asyncio transport buffer without application-level
backpressure.

The accepted shape drains after every body event whose `more_body` is true. The
application cannot produce the next chunk until asyncio's transport is below its
write-buffer threshold. The final event retains the existing exchange-level
drain, preserving the common one-event response path. Chunk framing and the final
terminator are unchanged.

This bounds queued streaming data without imposing a new public chunk-size knob.
Applications still choose event sizes; servery controls when the next event may
be produced. A single unusually large event can temporarily exceed the transport
high-water mark, but it is drained before control returns to the application.

The active socket timeout is not yet a reliable asyncio write-progress deadline.
An indefinitely stalled `drain()` holds one admitted connection/task but no
longer permits unbounded event production. A future `write_timeout` must cover
HTTP errors, final bodies, streaming bodies, and WebSockets consistently rather
than wrapping only one hot path.

## Rejected implementation shapes

Three correct but too-expensive variants were measured against the pre-change
image on CPython 3.15.0b3, one server CPU, 64 connections, seven rotated trials:

| Shape | ASGI 1 KiB throughput | p99 | Decision |
| --- | ---: | ---: | --- |
| `wait_for(drain(), timeout)` on every body event | -6.8% | +2.6% | reject |
| inspect transport high-water state on every event | -5.6% | +2.1% | reject |
| drain intermediate events; first bytecode shape | -7.4% | +1.5% | revise |

The final branch preserves the original final-event sequence and drains only the
`more_body` path. It measured +2.3% throughput / +2.3% p99 at 64 connections;
the result is neutral, not an improvement. A longer concurrency-one control from
the same design family measured -2.5% / +4.2%, also inside the protected budget.

## Streaming comparison workload

The external harness now includes identical WSGI and ASGI generators at 64 KiB
and 1 MiB, each emitted as 16 Content-Length-framed chunks. The current host
fixture is bind-mounted read-only into candidate, prebuilt baseline, Gunicorn,
and Uvicorn containers. This prevents an old image's embedded fixture from
silently changing application semantics during a paired source comparison.

At 64 KiB and concurrency 32, with two client processes:

| Pair | Throughput change | p99 change | Interpretation |
| --- | ---: | ---: | --- |
| unchanged WSGI control | -3.6% | +6.1% | host/tail noise control |
| ASGI with intermediate drains | 0.0% | +0.2% | neutral |

Clients used about 40% of their assigned CPU and all samples completed with zero
errors. The 1 MiB concurrency-16 run was inconclusive because both the unchanged
WSGI control and ASGI pair moved about -6% with broad dispersion. A lower-
concurrency rerun saturated its single client process and is retained only as a
client-limit diagnostic, not a server result.

## Slow-reader memory and cancellation gate

The harness also has an opt-in `asgi-slow-reader-64m` scenario. Four clients each
consume a 64 MiB, 1,024-event response in 16 KiB reads separated by 1 ms. The
application allocates a distinct deterministic 64 KiB body for each event; this
matters on modern asyncio transports, which can retain references to a reused
immutable body without copying all of its bytes. Status, total length, and SHA-256
are still checked before timing.

Three rotated paired trials compared the accepted implementation with the
pre-backpressure image on the same CPython 3.15.0b3 host:

| Implementation | Median cgroup peak | Median payload rate | Median p99 | Errors |
| --- | ---: | ---: | ---: | ---: |
| intermediate drain | 40.4 MiB | 54.1 MB/s | 4,961 ms | 0 |
| final drain only | 283.5 MiB | 53.9 MB/s | 4,980 ms | 0 |

Intermediate draining reduced the measured peak by 243.1 MiB, or 85.7%. The
paired candidate changes were -0.4% request rate and +0.4% p99, with about 10–11%
client CPU utilization. Request rate in this scenario is set primarily by the
intentional reader delay, so it is a correctness/memory control rather than a
server-throughput ranking. Cgroup peak includes runtime and kernel-accounted
memory, but the paired containers, workload, and CPU assignment are identical.

An earlier diagnostic reused the same immutable bytes object for every event and
both images peaked near 40 MiB. That result was useful fixture validation, not
evidence against backpressure: it measured reference retention rather than the
allocated chunks produced by typical file, database, or template generators.

A unit gate now blocks `StreamWriter.drain()`, verifies that the application's
intermediate `send()` remains pending, cancels it, and observes
`CancelledError`. This establishes that backpressure does not swallow task
cancellation. End-to-end peer-disconnect delivery and a bounded write-progress
deadline remain separate work.

Ignored artifacts under `benchmarks/artifacts/` are:

- `asgi-backpressure-paired-2026-07-10.json`;
- `asgi-backpressure-optimized-2026-07-10.json`;
- `asgi-stream-backpressure-paired-2026-07-10.json`;
- `asgi-stream-backpressure-v2-2026-07-10.json` (accepted 1 KiB gate);
- `asgi-stream-backpressure-c1-long-2026-07-10.json`;
- `dynamic-streaming-paired-2026-07-10.json`;
- `dynamic-streaming-c4-paired-2026-07-10.json`;
- `dynamic-streaming-64k-paired-2026-07-10.json` (accepted streaming gate);
- `asgi-slow-reader-memory-2026-07-10.json` (reused-body diagnostic);
- `asgi-slow-reader-allocated-memory-2026-07-10.json` (accepted memory gate).

## Follow-up

- Define a separate write-progress timeout and disconnect/error semantics.
- Add end-to-end disconnect delivery while an intermediate drain is blocked.
- Extend the same fixture contract to streaming request bodies and framework
  applications before making broad ASGI production claims.

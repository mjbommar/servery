# Lazy ASGI peer-disconnect delivery — 2026-07-11

## Problem

The ASGI HTTP adapter returned `http.disconnect` immediately after delivering
the final `http.request` event, even while the peer remained connected and before
a response existed. That made a long-poll or streaming application believe its
client had gone away. Conversely, a peer that actually closed while an
application waited after the request body had no end-to-end notification path.

Reading from `StreamReader` merely to observe EOF is unsafe. The byte may be the
first byte of a pipelined next request, and consuming it in a background listener
would fork parser ownership. Polling `reader.at_eof()` adds timer activity and
disconnect latency. A permanent watcher task or event on every request also
violates the measured minimal-ASGI hot-path budget.

## Accepted design

`_AsyncBody.receive()` still delivers request-body chunks exactly as before. Once
the final request event has been delivered, an additional `receive()` now blocks
until real stream EOF or connection loss. A disconnect during a declared or
chunked body is converted from `IncompleteReadError`/`ConnectionError` into an
ASGI `http.disconnect` event.

Observation is deliberately lazy:

1. `serve_forever()` continues to use the original `asyncio.start_server()` path.
2. The accepted stream protocol is passed through the connection exchange, but
   no event, future, set, watcher task, or wrapper is created for ordinary apps.
3. Only when an app calls `receive()` after the final body event does
   `_DisconnectState` temporarily wrap that protocol instance's
   `eof_received()` and `connection_lost()` callbacks.
4. The first real EOF wakes all registered receives without reading any bytes.
5. If an application cancels its listener first, the original protocol methods
   are restored. The keep-alive connection and pipelined bytes remain owned by
   the normal HTTP parser.

This closes real peer-disconnect delivery. Synthetic request-scope completion
was subsequently closed without reviving the eager designs by
[ASGI post-response disconnect](2026-07-11-asgi-post-response-disconnect.md).

## Rejected shapes

The first production shape replaced `start_server()` with a permanent
`StreamReaderProtocol` subclass and allocated two `asyncio.Event` objects per
request. A three-trial A/B measured -8.1% keep-alive RPS and -5.2% churn RPS.

A persistent queue-style disconnect state removed events and batched wakeups,
but still wired response completion into every response. Follow-up probes stayed
roughly 5–9% behind on keep-alive and increased churn p99 by 18–36%. A five-trial
gate for the best eager-response shape measured -5.7% RPS/+9.9% p99 on
keep-alive and -4.2% RPS/+29.8% p99 on churn. These variants are rejected.

The final lazy protocol hook removes all normal-close callbacks and all
response-state work. It pays for a future and temporary wrappers only in the
capability path that asks to observe disconnect.

## Correctness evidence

Direct tests prove that:

- the final request event is delivered once and a following receive remains
  pending while the peer is connected;
- closing the peer produces `http.disconnect` end to end;
- truncating a declared request body produces `http.disconnect`, not an internal
  read exception delivered past the adapter;
- cancelling a disconnect listener restores the original protocol methods;
- after that cancellation, two pipelined requests are both parsed and answered;
- existing keep-alive, TLS, body framing, streaming, timeout, and WebSocket tests
  continue to use the same adapter.

The tests run under both normal-GIL and free-threaded CPython 3.15. No application
polling interval or public configuration is introduced.

## Performance gate

The harness adds opt-in `asgi-churn-1k`, matching the `asgi-1k` response while
using one request per connection and a 32-connection cap. The final A/B uses one
server CPU, disjoint client CPUs, two client processes, one-second warmup, five
five-second trials, balanced order rotation, the same mounted application
fixture, and an immediately preceding frozen image.

Final artifact (gitignored):
`benchmarks/artifacts/asgi-disconnect-final-2026-07-11.json`.

| scenario | adapter | median RPS | RPS MAD | p99 | peak MiB |
|---|---|---:|---:|---:|---:|
| `asgi-1k`, 64 connections | lazy disconnect | 32,191 | 4.5% | 2.69 ms | 26.0 |
| `asgi-1k`, 64 connections | frozen baseline | 31,476 | 3.7% | 2.69 ms | 26.0 |
| `asgi-churn-1k`, 32 connections | lazy disconnect | 10,621 | 0.3% | 3.51 ms | 26.3 |
| `asgi-churn-1k`, 32 connections | frozen baseline | 10,641 | 0.5% | 3.53 ms | 26.4 |

The median paired keep-alive change is +4.1% RPS/+0.1% p99, but ratio MADs are
8.1 and 4.4 percentage points, so the decision is neutral rather than a gain.
Churn is tightly neutral at -0.3% RPS/-0.3% p99 with 0.6/0.5-point ratio MAD.
One frozen-baseline churn sample was an obvious high-throughput/high-p99 outlier;
medians, MAD, and paired ranges remain in the artifact rather than being removed.
All timed samples recorded zero errors and client CPU remained below saturation.

The artifact records candidate image ID `sha256:3fc70039...`, frozen baseline
image ID `sha256:587b8eff...`, and product-tree hash `a2b2bf5d...`.

## Decision and remaining work

Accept lazy real-peer disconnect delivery because it fixes a false-disconnect
bug, preserves parser ownership, restores hooks on cancellation, and is neutral
on both protected workloads. Keep the eager response-event designs rejected.

Before claiming full ASGI lifecycle parity, test Starlette/FastAPI cancellation
patterns, TLS EOF/half-close behavior, server-initiated shutdown, and
HTTP/2-to-ASGI if that adapter is added. Large streaming request bodies, native
Uvicorn cohorts, framework compatibility, and sustained lifecycle tests also
remain open roadmap items.

## Verification

- 800 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds. The normal environment has 40 optional-dependency skips; the populated
  free-threaded environment has four.
- Targeted ASGI tests also pass on normal-GIL CPython 3.14, including plaintext
  and TLS disconnect delivery.
- Repository-wide Ruff format/lint, Bandit, strict MkDocs, and `git diff --check`
  gates pass. Ty reports only two pre-existing unused-suppression warnings.
- The final benchmark artifact's product-tree and harness-file hashes match the
  current source.
- Wheel and source distribution build, the wheel has no unconditional runtime
  dependencies, and installed import/CLI smoke passes outside the source tree.

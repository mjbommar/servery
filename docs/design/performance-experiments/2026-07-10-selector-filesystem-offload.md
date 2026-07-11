# Selector filesystem offload — 2026-07-10

Status: accept the bounded executor as a benchmark control and possible
slow-storage policy; reject it as the selector default. Keep inline acquisition
for warm local files unless real deployment evidence supports opt-in offload.

## Problem

The selector prototype performs containment, open, fstat, MIME selection, and a
bounded small-file read on the event loop. Warm local operations are short, but
a page fault, remote filesystem, slow disk, permission service, or overloaded
mount can stop every connection. Blindly using `asyncio.to_thread()` avoids that
stall while introducing an implicit executor queue, per-request scheduling, and
unclear ownership when a connection is cancelled.

The experiment adds two explicit strategies to the benchmark-only prototype:

- `filesystem_workers=0`: perform acquisition inline, preserving the fast path;
- a positive worker count plus bounded queue: reject with `503` immediately when
  all worker/queue tokens are occupied.

No public servery setting is added. The controls are inputs to the architecture
decision if a selector backend is ever promoted.

## Ownership and shutdown design

One preparation operation owns path open, fstat, regular-file validation, MIME
selection, and the small-body read. It returns an opened handle plus immutable
facts; the response emitter closes the handle exactly once.

Executor futures are shielded from connection-task cancellation because Python
cannot stop an already-running filesystem syscall. If the connection is
cancelled, a completion callback closes the late result. Executor capacity is
released only when the worker actually finishes, not when the waiter disappears.
Graceful server shutdown first drains/cancels connection tasks, then awaits
outstanding preparation futures and shuts down the executor.

Tests prove successful pooled acquisition, immediate saturation rejection and
recovery, cancellation accounting, late-result close, invalid-policy rejection,
and normal inline behavior.

## Warm local-file cost

CPython 3.15.0b3, one server CPU, five rotated three-second trials, four workers
plus 64 bounded queue slots:

| Workload | Inline prototype | Four-worker pool | Pool change |
| --- | ---: | ---: | ---: |
| 1 KiB, 64 keep-alive | 19.88k RPS / 4.06 ms p99 | 13.95k / 6.93 ms | -29.8% RPS / +70.7% p99 |
| 1 KiB, 32 churn | 8.88k RPS / 4.31 ms p99 | 7.71k / 5.43 ms | -13.3% RPS / +26.0% p99 |

The pool erases the selector's keep-alive throughput advantage over threaded
production and consumes scheduler/executor work even when every file is already
hot. It fails the protected 5% regression budget and is rejected as default.

## Controlled slow-filesystem result

To isolate blocking behavior without perturbing the host page cache, the same
preparation function was given a deterministic 10 ms delay. This is a scheduling
probe, not a claim about a particular disk or NFS deployment.

| Strategy | RPS | p99 | Peak memory |
| --- | ---: | ---: | ---: |
| Inline | 98 | 714.8 ms | 25.8 MiB |
| 4 workers + 64 queue | 394 | 162.8 ms | 25.9 MiB |
| 16 workers + 64 queue | 1,576 | 41.4 ms | 27.9 MiB |

The result follows the expected concurrency ceilings almost exactly. Bounded
offload prevents one slow lookup from serializing the event loop and scales with
the chosen worker budget, while memory remains controlled. The worker count is a
real resource/latency policy, not an internal implementation detail.

Ignored artifacts under `benchmarks/artifacts/`:

- `selector-filesystem-warm-2026-07-10.json`;
- `selector-filesystem-slow-2026-07-10.json`.

## Decision

- Keep inline preparation as the prototype default for local warm-cache serving.
- Retain the bounded pool as an explicit benchmark mode for real slow-storage
  tests; never fall back to an unbounded default executor.
- If a selector backend becomes public, expose filesystem workers and queue only
  as operator policy or a clearly documented slow-storage profile. Do not guess
  automatically from one observed slow request and migrate traffic mid-run.
- A saturated pool returns `503` and a close rather than queueing beyond budget.
- Before promotion, repeat on cold local storage, rotational media, NFS/SMB/FUSE,
  macOS, and Windows, including cancellation during actual blocked syscalls.

This resolves the architecture question for warm files but not the deployment
question. A selector mode that is fast only when all metadata is hot still needs
an honest documented scope or an opt-in bounded offload policy.

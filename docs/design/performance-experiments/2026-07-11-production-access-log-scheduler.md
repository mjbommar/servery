# Production bounded access-log handoff — 2026-07-11

Status: accept the bounded writer as an explicit production capability; retain
synchronous file logging as the default. The lossless async candidate failed the
protected p99 gate, and the lossy candidate dropped most records under the
measured saturation workload.

## Implementation

`AsyncAccessLog` composes the existing formatter/file owner with one dedicated
writer thread. It provides:

- exact active-plus-queued record capacity and a separate retained-byte budget;
- `block` backpressure or nonblocking `drop` overload policy;
- FIFO enqueue order and configurable batch size/window;
- immutable records whose timestamp is captured at response commit;
- a single file-handle owner, first-error latching, and later-record rejection;
- finite shutdown drain with accepted/written/failed/abandoned/drop accounting;
- count, byte, active, queue, delivery, and failure high-water snapshots;
- CLF/combined escaping for quotes, backslashes, and control characters.

The shipped configuration is explicit:

```console
# Existing synchronous behavior and current default
servery --access-log access.log --access-log-queue 0

# Lossless bounded handoff; response threads backpressure at saturation
servery --access-log access.log --access-log-queue 256 \
  --access-log-overflow block

# Preserve response progress at the cost of explicitly counted log loss
servery --access-log access.log --access-log-queue 256 \
  --access-log-overflow drop
```

Queue count, queue bytes, overflow, batch size/window, and drain timeout are
separate controls. A record larger than the total byte budget is rejected even
under `block`, avoiding an impossible indefinite wait. A stuck filesystem write
cannot be interrupted safely in a Python thread; finite close returns an
incomplete snapshot and the worker-process supervisor remains the hard deadline.

Focused tests cover exact count and byte boundaries, blocking/unblocking, drops,
batch delivery, sink failure and queued abandonment, close-versus-producer races,
bounded close, concurrent producer stress, escaping, config validation, and
synchronous/async server selection. Focused branch coverage is 97%.

## Scope boundary

The integration currently covers the existing HTTP/1 file-serving access hook.
WSGI, ASGI, CGI/proxy, HTTP/2, and HTTP/3 still emit diagnostic request logs but
do not all feed this file sink. Multiworker file logging remains rejected until
the parent can own one bounded aggregation channel. The CLI and guide state this
boundary; this record does not call the file an all-transport audit log.

## Controlled benchmark

The `static-access-log-1k` cohort used:

- CPython 3.15.0b3 with the GIL;
- one server CPU (logical CPU 0, physical core 0);
- two client processes pinned to logical CPUs 2 and 4 (physical cores 1 and 2);
- 64 persistent connections, one-second warmup, three-second timed runs;
- seven deterministic rotations, exact response probes, zero response errors;
- out-of-band post-trial line counts and cgroup peak memory.

The candidate image was
`sha256:f58190f51add57e41ac7b86f0e4ac3949b8f2ab63504420308336d97a6ed0f58`;
the product tree hash was
`89c25d4b6b2f5b58e7f468eee939ee997c86ecc091c1875037b42eac7eafac33`.
Unlike the earlier selector experiment, server and client sets do not share a
physical core.

### Initial policy comparison

| Policy | Median RPS | RPS MAD | p99 | Peak MiB | Delivery |
| --- | ---: | ---: | ---: | ---: | ---: |
| unlogged | 19,629 | 0.6% | 12.85 ms | 30.6 | n/a |
| synchronous | 13,374 | 0.6% | 5.80 ms | 34.7 | 100% |
| async `block`, queue 256, batch 8/1 ms | 12,915 | 0.5% | 21.22 ms | 34.6 | 100% |
| async `drop`, queue 256, batch 8/1 ms | 16,952 | 0.3% | 12.57 ms | 34.2 | 25.9% |

The lossless async shape is 3.4% lower in throughput and 266% higher in p99 than
synchronous logging. It meets the provisional 5% throughput budget but fails the
10% p99 budget decisively. Drop increases logged-server throughput by 26.8%, but
74% record loss makes it an overload choice, not an audit-log default.

### Batch and queue tuning

| Policy | Median RPS | RPS MAD | p99 | Delivery |
| --- | ---: | ---: | ---: | ---: |
| synchronous control | 13,379 | 1.0% | 5.92 ms | 100% |
| `block`, queue 256, batch 64/no wait | 15,519 | 0.5% | 16.36 ms | 100% |
| `drop`, queue 256, batch 64/no wait | 17,039 | 0.3% | 12.36 ms | 30.5% |
| synchronous second control | 12,965 | 0.5% | 5.92 ms | 100% |
| `block`, queue 8, batch 8/no wait | 7,870 | 0.5% | 34.12 ms | 100% |
| `block`, queue 32, batch 32/no wait | 12,951 | 0.6% | 17.64 ms | 100% |

Large batches recover 16.0% throughput over the paired synchronous control but
still increase p99 by 176%. Small queues increase how often request threads
contend on the queue condition and perform worse. No measured async-block policy
passes both protected gates.

Ignored raw artifacts:

- `benchmarks/artifacts/production-access-log-2026-07-11.json`;
- `benchmarks/artifacts/production-access-log-tuning-2026-07-11.json`;
- `benchmarks/artifacts/production-access-log-queue-tuning-2026-07-11.json`.

## Decision and next gate

- Keep synchronous logging as the default (`access_log_queue = 0`).
- Ship bounded async `block` and `drop` as explicit operator policies because
  they bound slow-sink memory and make the loss-versus-backpressure tradeoff
  honest and configurable.
- Do not select async logging in a production profile from this evidence.
- Add the shared response-commit hook before claiming dynamic/H2/H3 coverage.
- Run controlled slow/full sink, free-threaded, multi-CPU, and shutdown-saturation
  cohorts. `drop` must demonstrate cheap-request progress and exact accounting;
  `block` must plateau rather than grow memory.
- Parent-owned multiworker aggregation remains an `EDGE-013` state-ownership
  dependency.

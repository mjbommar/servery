# Worker recovery and state ownership checkpoint (2026-07-11)

## Status

`EDGE-013` is **in progress**, not accepted. Crash recovery and age-based
recycling are being implemented separately. This checkpoint defines the state
and resource contract around that work and deliberately keeps unsafe
multi-worker combinations rejected.

The fixed-generation supervisor can safely multiply stateless static, WSGI,
and ASGI workers. It cannot make process-local locks, counters, caches, file
handles, UDP listeners, or certificate issuance global merely by transferring
the TCP listener.

## Shipped resource semantics

The following settings are budgets for each worker process:

| Setting/state | Per-worker meaning | Process-tree upper bound with `N` workers |
| --- | --- | --- |
| `max_connections` | admitted HTTP/TLS connections, or ASGI sessions | `N * max_connections` |
| `max_workers` | blocking/threaded executor threads | `N * max_workers` when configured |
| `compression_cache_size` | retained encoded response bytes | `N * compression_cache_size` |
| digest cache | zero retained entries; same-key work coalesces only inside one worker | zero retained entries, but up to one same-key digest flight per worker |
| application globals/lifespan state | one independent copy | `N` independent copies |

`Config.aggregate_connection_limit` and
`Config.aggregate_compression_cache_size` expose the two exact byte/admission
calculations for the committed fixed generation. CLI and reference text call
the underlying values per-worker. This multiplication is a predictable
steady-state bound, not a claim that load is distributed evenly.

Zero-downtime recycling deliberately creates a bounded overlap: prepare one
replacement, commit it, and only then drain the selected old worker. During
that interval there can be `N + 1` processes and therefore up to
`(N + 1) * compression_cache_size` retained compressed bytes, plus both
workers' application/lifespan state. The replacement does not accept before
commit, so connection admission grows only after the old worker begins closing
keep-alive admission; nevertheless existing old connections and new replacement
connections can overlap. Production memory sizing must reserve one worker of
headroom. Crash replacement starts only after the crashed process is reaped and
does not require this live-worker overlap.

There is no parent accept semaphore today. Adding one would provide a precise
global connection cap but would also put IPC and a parent scheduling decision
on every accepted connection. Until measurements justify that cost, operators
divide their desired process-tree limit by the configured worker count.

Uploads, resumable partial counts, target locks, and DAV locks are process-local
in the one-worker runtime. Multi-worker configuration rejects those modes, so
their configured limits remain service-global in every configuration that is
currently valid. Servery does not silently multiply a write budget.

## Singleton decisions

The preflight validator rejects every mode below for `workers > 1`. These are
intentional correctness boundaries rather than missing dispatch cases.

| Mode | Why it remains rejected | Required owner before enabling |
| --- | --- | --- |
| file access log | concurrent cross-platform appends do not guarantee whole-record ordering or atomicity; rotation would race | parent bounded record queue and sole file handle, with explicit block/drop policy |
| ACME | initial issuance can occur in the parent, but renewal, challenge ownership, and atomic identity rollover are not implemented | parent certificate manager with a single renewal schedule and generation handoff |
| mDNS and QR | advertisement/startup output must describe one committed generation and stop exactly once | parent lifecycle owner after commit |
| TFTP | separate UDP listener and transfer pool need one lifecycle owner; write mode also shares targets with HTTP | parent listener/transfer service plus the write broker below |
| HTTP/3 | one UDP/QUIC endpoint needs connection ownership and coordinated certificate/config generations | one parent-owned H3 runtime initially; measured sharding may follow |
| upload and WebDAV | in-memory target leases, partial counts, and DAV token tables are not shared | bounded parent state broker |
| CGI and proxy | child-process/upstream work budgets and health state are not global | explicit global admission/health policy; not enabled by accident |

Read-only WebDAV is also rejected. Although it does not mutate file content,
LOCK/UNLOCK semantics and a coherent advertised DAV capability still depend on
one lock table. Splitting read-only and lock-capable DAV is a later product
decision, not a validation loophole.

## Proposed standard-library state broker

This is a design target; no restriction is lifted by this document.

The supervisor parent owns a bounded request channel per worker and these
tables:

- target leases keyed by a canonical parent-directory identity plus basename,
  so a not-yet-created target has a stable key;
- DAV locks keyed by canonical resource identity, including token, depth,
  owner, expiry, and generation;
- aggregate active-upload bytes/count and partial-upload reservations;
- singleton access-log queue and lifecycle handles.

Every acquisition returns an unguessable lease ID bound to worker ID and worker
generation. Release is idempotent. The parent revokes all leases and
reservations for a worker when its control channel reaches EOF or the process is
reaped, so a dead worker cannot strand state. A monotonic expiry bounds the
effect of a parent-side missed notification. Queue capacity, reply timeout, and
table sizes are configured and saturation fails closed before a request body is
consumed.

A write flow is:

1. Resolve and validate the parent directory without following an untrusted
   final symlink.
2. Reserve global upload count/bytes, then acquire the canonical target lease.
3. Stream to a private temporary file while enforcing the reserved byte bound.
4. Revalidate containment and atomically publish under the lease.
5. Release the lease and unused reservation in `finally`; parent owner-death
   cleanup is the backstop.

DAV tokens cannot be stored only in a replacement worker. The parent table
survives worker recycling; a deliberate supervisor restart may lose volatile
locks and must advertise that limitation until persistence is designed.

## Recovery-policy configuration target

The design reserves these typed policy fields, but they are not exposed by the
CLI or `Config` until the recovery implementation passes its lifecycle gates:

- `restart_backoff_initial=0.25` seconds and `restart_backoff_max=30` seconds;
- `worker_restart_limit=5` in `worker_restart_window=60` seconds, with zero
  restart limit disabling crash replacement;
- `worker_max_age=0` seconds, so age recycling is disabled by default;
- `worker_max_age_jitter=0.1`, constrained to `[0, 1]` and inert while maximum
  age is zero.

Future validation must prevent zero/negative timing loops, an inverted backoff
interval, negative restart limits, and invalid jitter. Configuration alone is
not evidence that recovery is correct; bounded crash/hang/recycling tests remain
an acceptance requirement.

## Remaining acceptance gates

`EDGE-013` must stay open until all of these are demonstrated:

- crash, startup-failure, hang, crash-loop, and recycling behavior remains
  bounded and leaves the required worker count or an explicit failed service;
- request-count recycling exists in addition to optional age recycling;
- simultaneous same-target writes through different workers serialize;
- killing a lease holder releases its target, upload reservation, and DAV
  state without waiting forever;
- each enabled singleton starts once, changes generation atomically, and stops
  once;
- global queue/table/byte saturation and parent failure have deterministic,
  tested outcomes on POSIX and Windows spawn semantics.

Until those gates pass, the honest production surface is multi-worker
read-only static, WSGI, or ASGI serving, with process-local caches and resource
budgets.

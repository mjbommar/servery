# Production capability gap

Status: superseded as product direction on 2026-07-11. This remains a capability
inventory, but its recommendation to deploy behind another edge is no longer the
selected target. The actionable direct-edge scope and task ordering are in the
[Production-edge execution backlog](production-edge-execution-backlog.md).

## Historical alternatives and selected product decision

Modern CPython narrows the raw-performance gap, and servery already has a strong
small-server feature set. It does not erase the operational gap. Mature edge and
application servers are differentiated by process isolation, reloads, traffic
policy, certificate lifecycle, observability, shared state, ecosystem
compatibility, and years of hostile-input testing—not only by how quickly they
write bytes to a loopback socket.

There are three coherent targets:

| Target | Sensible scope |
| --- | --- |
| **Ad-hoc/LAN server** | Keep the current zero-dependency, one-process model and generous/off policies. It remains useful independently of production work. |
| **Direct public edge** | The selected target: one host and configuration, public TLS, HTTP negotiation, local supervision, bounded overload, renewal, and operations owned by servery. |
| **General edge platform** | Deferred: upstream clusters, arbitrary multi-site routing, distributed coordination, and shared proxy caches. |

The selected target is now a deliberately bounded form of the third option: a
directly exposed, single-service production edge that requires only Python and
servery for HTTP/1.1, HTTP/2, TLS, supervision, metrics, and certificate
lifecycle. It does not attempt every general-purpose proxy, cache, or distributed
traffic-management feature.

## Decision rules

- Protocol correctness, safe parsing, atomic writes, and trustworthy proxy
  identity are invariants. Do not offer flags to weaken them silently.
- Capacity, timeout, cache, retry, rate, worker, and certificate-renewal choices
  are deployment policy. Put them in the library `Config` first, then expose the
  operator-relevant subset through CLI/config-file settings.
- Preserve small/simple paths. For example, keep buffered `read()` for bounded
  small responses and streaming/sendfile for large responses; do not impose a
  universal streaming state machine merely because it has better worst-case
  memory.
- Optional production machinery should not compromise the zero-dependency core.
  A production profile can assemble stdlib features, while integrations that
  inherently need external systems belong in explicit extras or adapters.
- Every new fast path needs a portable fallback and differential tests proving
  identical HTTP behavior.

## Ranked capabilities

### P0 — prerequisites for an honest production claim

1. **Production threat model, protocol fuzzing, and a security-response policy.**
   Add coverage-guided fuzz targets for HTTP/1 framing, HPACK/HTTP/2 state,
   WebSocket frames, multipart, archive extraction, WebDAV XML, and ACME data;
   run differential/interoperability suites and define supported security-fix
   windows. This is not configurable. The tradeoff is ongoing CI time and triage,
   but without it “production ready” is a marketing claim rather than an
   engineering property.

2. **Multi-process supervisor and worker lifecycle.** Add
   `workers = 1|N|auto`, crash detection, bounded restart backoff, startup
   readiness, graceful termination, and optional request-count/age recycling.
   Keep one process as the default for the ad-hoc profile. Multiple processes use
   more memory and force shared-state decisions, but provide CPU scaling, crash
   isolation, and recovery that threads alone cannot.

3. **Graceful reload and zero-downtime replacement.** Support validated config
   and certificate reload, listener inheritance or reuse, connection draining,
   and a maximum drain deadline. On Unix this can use signals and inherited file
   descriptors; Windows needs a spawn-and-handoff design. Make drain time and
   forced-close policy configurable, while never serving a half-applied config.

4. **Layered admission control and rate limiting.** Build on the existing
   connection/body limits with header-byte/count limits, header/read/write
   deadlines, per-client and global request/token buckets, expensive-operation
   budgets, queue limits, and a deterministic overload response. Limits belong
   in a resource-policy object and production profile; identity parsing and
   request framing remain fixed. Very low defaults harm LAN bulk transfer, so
   rate and bandwidth policies should be off or generous outside production.

5. **Operational observability.** Add bounded-cardinality counters/histograms,
   structured error/event logs, request IDs, active-worker/connection gauges,
   cache/proxy/ACME state, and explicit liveness/readiness endpoints. A stdlib
   Prometheus text endpoint is plausible; OTLP or vendor exporters should be
   adapters. Metrics should be configurable by endpoint/bind and never expose
   filenames, credentials, or unbounded path labels by default.

6. **Continuous certificate lifecycle.** Current ACME acquisition refreshes an
   old cached certificate at process startup. A direct public edge also needs a
   background renewal scheduler, expiry-based timing with jitter, retry/backoff,
   atomic durable writes, hot `SSLContext` replacement, multi-domain status, and
   expiry alerts. Expose `renew_before`, storage, challenge mode, and failure
   policy; default to keeping a still-valid certificate if renewal temporarily
   fails. This adds long-lived state, but it is required because servery owns TLS
   in the selected direct-edge topology.

### P1 — closes the major nginx/Caddy/Gunicorn/Uvicorn capability gaps

7. **Real upstream pools and failure handling.** Evolve one target per proxy
   prefix into named pools with connection reuse, least-connections/round-robin
   policies, active and passive health checks, outlier ejection, bounded pending
   work, and circuit breaking. Retry count/time budget must be configurable and
   default to idempotent requests only; replaying a partially sent POST is not a
   performance choice but a correctness error.

8. **Bounded reverse-proxy cache.** Add standards-aware memory/disk tiers,
   revalidation, `Vary`, stale-while-revalidate/stale-if-error, cache locks, and
   atomic eviction. Default it off. Budgets, eligible status/method rules, maximum
   object size, TTL ceilings, and stale policy are configuration. A disk cache
   improves origin protection but creates invalidation, privacy, and storage
   maintenance obligations; it should not share the existing in-process
   compression cache accidentally.

9. **Explicit proxy trust and deployment sockets.** Add Unix-domain listeners,
   systemd socket activation, optional PROXY protocol, and trusted-CIDR handling
   for `Forwarded`/`X-Forwarded-*`. Trust no forwarding header by default. The
   allowlist and accepted header family are configurable; chain parsing and
   spoofing resistance are invariants. This is required for correct client IP,
   scheme, rate-limit, and audit behavior behind another proxy.

10. **ASGI/WSGI production compatibility gates.** Maintain conformance tests for
    lifespan, disconnects, streaming bodies, trailers where supported,
    WebSockets, slow consumers, cancellation, proxy headers, and framework test
    applications. Add HTTP/2-to-ASGI delivery if the product wants parity with
    dedicated async servers. WSGI and ASGI should stay separate modes; a flag
    cannot make blocking WSGI code asynchronous. Document spec versions and
    unsupported extensions rather than accepting them approximately.

11. **Selectable event-driven connection backend.** A selector-based HTTP/1
    frontend can reduce idle-connection thread stacks and context switching,
    while bounded workers handle filesystem and application blocking work. Keep
    `connection_backend = threaded|selector` configurable until benchmarks prove
    one default across Linux, macOS, and Windows. The threaded path is simpler and
    can be faster for modest concurrency; an event loop adds state-machine and
    cancellation complexity and does not make disk I/O nonblocking by itself.

12. **Cross-process coordination and shared state.** Worker processes need
    coherent write exclusion, WebDAV locks, rate counters, cache-stampede control,
    and graceful-reload state. Define a narrow coordinator interface with a
    process-local default and a same-host implementation, potentially SQLite or
    OS file locks with platform-specific fallbacks. External Redis-style state
    should be an adapter. Strong consistency costs latency; each feature must state
    whether approximate counters are safe or exclusive locking is required.

### P2 — operational depth and last-mile efficiency

13. **Validated config files, environment overlays, and secret indirection.** Use
    stdlib TOML input with a documented precedence such as defaults < profile <
    file < environment < CLI. Validate the complete candidate before bind/reload,
    support `--check-config`, and permit credentials/passphrases via protected
    files or environment references rather than command lines. More sources make
    debugging harder, so the startup report should show effective non-secret
    configuration and provenance.

14. **Privilege separation and hardened service packaging.** Add Unix user/group
    drop after bind, restrictive umask, optional chroot-like root handling where
    portable, no-new-privileges guidance, read-only filesystem compatibility,
    systemd launch/hardening examples, container health checks, and orderly
    PID/status behavior. Keep OS-specific controls opt-in and fail closed when
    requested; pretending a control applied is worse than lacking it.

15. **Static-origin cache efficiency.** Add validated precompressed sidecars,
    open-file/metadata caching with inode/mtime invalidation, configurable cache
    budgets, and measured decisions among small `read()`, buffered streaming, and
    `sendfile`. Do not cache open descriptors or whole bodies without limits.
    Precompressed files save CPU but complicate deployment freshness; on-the-fly
    compression remains useful for ad-hoc use.

16. **Traffic shaping and fair scheduling.** Add global, per-client, and
    per-transfer bandwidth ceilings plus fair service between large downloads,
    small requests, proxy traffic, HTTP/2 streams, and write operations. Keep
    shaping off by default because token accounting and extra wakeups reduce peak
    throughput. Configure observable byte/time budgets, not low-level chunk sizes,
    unless benchmarks demonstrate an operator need.

## Suggested sequence

For the selected direct-edge goal, the executable ordering and first-release
deferrals are now maintained in the
[Production-edge execution backlog](production-edge-execution-backlog.md).
Continuous certificate lifecycle is required. General upstream pools and a
reverse-proxy disk cache remain deferred because a single-service edge does not
need them. Implement the selector, static-cache, and traffic-shaping work only
from external benchmark evidence, not from the assumption that another server's
architecture must be copied wholesale.

The comparison suite in
[External server comparison benchmarks](server-comparison-benchmarks.md) supplies
the performance evidence. The
[Performance and production-gap research roadmap](performance-gap-research-roadmap.md)
defines the staged investigation, prototype gates, and protected performance
budgets before implementation. The earlier
[Reliability and performance priorities](reliability-performance-priorities.md)
document records the bounded-buffering and resource-policy work already shipped;
those solved important correctness and efficiency issues, but they are not a
substitute for the operational capabilities above.

# Production-edge execution backlog

Status: accepted product direction; actionable backlog as of 2026-07-11.

This document converts the production-edge goal into bounded tasks that can be
assigned to independent agents. It supersedes the earlier recommendation that
servery should normally rely on Caddy or nginx for its public edge.

## Product target

Servery should deploy one production service on one host with Python and a very
small package set:

```console
python -m pip install servery
servery --profile production --config servery.toml
```

Servery owns the public sockets, TLS and certificate lifecycle, worker lifecycle,
HTTP serving, overload behavior, and operational endpoints. It may serve static
content, one WSGI application, or one ASGI application. The production claim
must not require another edge proxy, process manager, state service, or telemetry
agent.

The first production release does not need arbitrary routing languages,
upstream clusters, Kubernetes ingress behavior, distributed coordination, or a
general shared disk proxy cache.

### Dependency and safety budget

- The default wheel retains `project.dependencies = []`.
- HTTP/1.1, HTTP/2, TLS, ACME, supervision, metrics, and configuration work with
  servery plus the standard library.
- HTTP/3 remains optional. `aioquic` is one direct dependency but installs
  transitive distributions, so its environment must not be described as
  literally containing only two installed packages.
- Development-only test, benchmark, fuzz, and documentation packages do not
  count against the deployed runtime.
- No new native extension enters the default path. The honest claim is
  memory-safe server logic in Python on CPython/OpenSSL, not that every layer
  below Python is memory-safe.

## Initial production scope

- One host and one servery configuration.
- Multiple supervised local worker processes.
- Static, WSGI, or ASGI serving, including streams and WebSockets.
- HTTPS with HTTP/1.1 and HTTP/2; HTTP/3 is a separate optional gate.
- Automatic certificate acquisition and continuous renewal.
- Bounded overload, readiness, metrics, graceful deploy, and crash recovery.
- Linux is the first required production platform. Existing portable modes must
  not regress; macOS and Windows production claims require their own lifecycle
  gates rather than Unix assumptions.

### Frozen first-release contract

This is the authoritative contract for the first production-edge release. It
describes a release target, not the capabilities of the current build.

| Dimension | First production claim |
| --- | --- |
| Topology | One host, one configuration, one parent supervisor, and one or more local workers. The process owns its TCP/TLS listeners and needs no external proxy, process manager, state store, or telemetry service. |
| Platform | Linux on every CPython version supported by the release. macOS and Windows retain supported portable single-process modes, but are not called production platforms until their supervisor, reload, and failure gates pass. |
| Public protocols | HTTPS over HTTP/1.1 and HTTP/2. Plain HTTP may redirect to HTTPS or serve ACME HTTP-01. HTTP/3 is optional and is not a release gate. |
| Application modes | Exactly one of static, WSGI, or ASGI per service. WSGI includes iterable responses; ASGI includes lifespan, HTTP streaming, background work, and WebSockets. CGI, TFTP, mDNS, WebDAV writes, and the general reverse proxy are not in the first public-production claim. |
| Configuration | One validated configuration file, with documented environment and explicit CLI overrides. Invalid configuration or application startup fails before readiness. |
| Certificates | Operator-provided certificates or unattended ACME acquisition and renewal, with atomic persistence and live replacement. Ad-hoc self-signed certificates are development-only. |
| Deployment | Start, inspect, reload, rollback, renew, and stop are supported directly by servery. OS service definitions may launch servery, but correctness and worker recovery do not depend on their supervision. |

The HTTP/2 application adapter is part of this target: a service must not
advertise `h2` and then silently lose its selected WSGI or ASGI application.
Until that adapter passes its release gates, dynamic production configurations
must advertise HTTP/1.1 only and documentation must describe that limitation.

### Lifecycle meanings

- **Live:** the parent control loop is responsive and can report state. A live
  service can be starting or draining and is not necessarily safe for traffic.
- **Ready:** the complete configuration is validated, required listeners are
  owned, the minimum worker set is healthy, application import and ASGI startup
  have succeeded, and the service can accept new work. Only ready generations
  receive new traffic.
- **Draining:** admission of new connections and new keep-alive/H2 work has
  stopped for that generation; accepted HTTP responses, streams, background
  work, and WebSockets may run until the configured deadline. Draining is live
  but not ready.
- **Failed:** the service cannot meet its configured minimum healthy-worker or
  certificate/listener requirements, or entered a bounded crash loop. Failed is
  neither ready nor eligible for traffic; it may remain live long enough to
  report the cause and perform bounded cleanup.

Readiness is generation-specific. A replacement generation becoming ready is
what permits the previous generation to enter draining; merely spawning a
process never establishes readiness.

### Measurable initial objectives

Release evidence must use the exact wheel or sdist-built wheel and record the
host, Python, configuration, workload, and raw probe results.

- **Availability and deploy:** 100 consecutive generation reloads under mixed
  static/ASGI/WebSocket load produce no incorrect response and no failed 10 Hz
  availability probe; a worker crash is replaced within 5 seconds and causes no
  probe outage longer than 1 second.
- **Graceful behavior:** work accepted before drain either completes or is
  reported by type as forcibly terminated. Admission closes within 1 second;
  forced cleanup finishes within 1 second after `drain_timeout`; repeated stop
  is idempotent and leaves no child, task, listener, or temporary file.
- **Bounded memory and work:** every connection, stream, queue, upload,
  compression, digest, archive, and background-work class has an enforced
  capacity. Under a configured cgroup memory ceiling and twice the admitted
  concurrency, the service rejects observably rather than being OOM-killed;
  after an eight-hour mixed-load soak, post-warmup RSS growth is at most the
  greater of 5% or 32 MiB.
- **Overload and recovery:** saturation never grows a configured queue beyond
  its limit, every rejection has a stable reason counter, and normal success
  rate and readiness recover within 5 seconds after offered load returns below
  capacity.
- **Renewal:** injected ACME failures use bounded jittered retry, never replace a
  valid certificate with an invalid one, alert before expiry, and recover before
  the test certificate expires. Successful renewal is atomically persisted and
  served to new TLS handshakes within 60 seconds without a failed availability
  probe.

These are floors, not promises that Python, CPython, OpenSSL, application code,
or the operating system cannot contain memory-safety defects. The accurate
description is a memory-safe Python implementation with a small native/runtime
surface; servery does not claim that the complete executable stack is written in
a memory-safe language.

## Release definition of done

- [ ] A fresh host reaches a serving HTTPS endpoint from one documented command
      and one configuration file, with no external proxy or process manager.
- [ ] Worker crash, deploy, reload, and renewal do not interrupt the availability
      probe beyond the agreed error budget.
- [ ] Sockets, queues, memory-sensitive work, and expensive operations remain
      bounded under overload, with observable rejection.
- [ ] HTTP responses, application background work, streams, and WebSockets drain
      to a configurable deadline before forced termination.
- [ ] HTTP/1.1 and HTTP/2 interop, fuzz, soak, failure-injection, package, and
      controlled performance gates pass against the exact release artifact.
- [ ] The wheel and an sdist-built wheel pass outside-tree installed smokes.
- [ ] The security-response policy and supported production platforms are public.

## Rules for agent tasks

Each task has one owning agent and should leave a design or experiment record,
implementation, focused failure-path tests, configuration/default rationale,
relevant resource or performance evidence, documentation, and exact verification
commands. Research-only tasks must turn unknowns into an accepted decision or
explicitly blocked implementation task.

Protocol correctness, safe framing, path containment, atomic replacement, and
distrust of forwarding headers are invariants. Worker count, deadlines, capacity,
rates, renewal timing, recycling, and telemetry binds are policy: define them in
`Config` before CLI or TOML.

Status notation is `[ ]` pending, `[~]` active, `[x]` accepted with evidence,
`[-]` rejected with a recorded reason, and `[!]` blocked with a named blocker.

## Dependency graph

```text
EDGE-001 product contract
  |-- EDGE-002 threat model
  |-- EDGE-003 configuration schema
  |-- EDGE-004 metrics vocabulary
  |-- EDGE-010 graceful drain
  `-- EDGE-020 selector decision

EDGE-010 + EDGE-011 listener seam
  `-- EDGE-012 supervisor
        |-- EDGE-013 recovery/recycling
        `-- EDGE-014 zero-downtime reload

EDGE-003 + EDGE-004 + supervisor state
  |-- EDGE-030 resource policy
  |-- EDGE-040 administration listener
  `-- EDGE-050 certificate store/renewal

EDGE-020 + EDGE-021 bounded offload
  `-- EDGE-022 backend parity

all implementation tracks
  `-- EDGE-060..065 release assurance
        `-- EDGE-070 production profile
```

## Wave 0 — contract and baseline

### [x] EDGE-001 — freeze the production-edge contract

**Owner:** coordinator/product agent. **Depends on:** none.

- Define supported topology, platforms, protocols, application modes, and
  explicit non-goals.
- Define measurable availability, graceful-deploy, memory, overload, and renewal
  objectives plus the meanings of ready, live, draining, and failed.
- Audit every production, stable, and memory-safety claim for accuracy.

**Accept when:** one release checklist defines the direct-edge claim and all
contradictory older guidance is removed or marked historical.

**Evidence (2026-07-11):** the frozen contract, lifecycle meanings, measurable
objectives, dependency/safety language, and release checklist above are the
authoritative direct-edge claim. `VISION.md`, `PRINCIPLES.md`, `REQUIREMENTS.md`,
`DYNAMIC.md`, and the production-gap roadmap now distinguish the current build
from that target and no longer require nginx, Caddy, or an external process
manager for public production operation.

### [x] EDGE-002 — threat model and hostile-input inventory

**Owner:** security agent. **Depends on:** `EDGE-001`.

- Inventory HTTP/1 framing, HPACK/H2, TLS/ALPN, ACME, WebSocket, multipart,
  WebDAV, archives, proxy requests, paths, configuration, and expensive work.
- Model slow clients, floods, rapid resets, compression/digest amplification,
  upload exhaustion, symlink races, cache poisoning, crash loops, and failed
  certificate renewal.
- Map every threat to a control, metric, test, owner, or explicit gap.
- Define supported security versions and vulnerability-response expectations.

**Accept when:** every public parser and expensive operation has a disposition
and all unknowns have named follow-up tasks.

**Evidence (2026-07-11):** the
[production-edge threat model](production-edge-threat-model.md) inventories the
public HTTP/1, H2/HPACK, TLS/ALPN, optional H3, ACME, WebSocket, multipart,
resumable-upload, WebDAV, archive, proxy, path, configuration, application, CGI,
and TFTP boundaries plus expensive filesystem/compression/digest/listing work.
Every row names current controls, residual task IDs, bounded operational
signals, and a verification owner. Cross-cutting slow/flood/reset/amplification,
disk, race, cache, crash, renewal, observability, and lifecycle scenarios have
release dispositions. Repository `SECURITY.md` now defines supported versions,
private reporting, response targets, disclosure, and upstream dependency
boundaries. Strict MkDocs and `git diff --check` pass.

### [x] EDGE-003 — typed production configuration design

**Owner:** configuration agent. **Depends on:** `EDGE-001`.

- Design typed runtime, listener, TLS/ACME, resource, logging, metrics, and
  application sections.
- Set precedence to defaults < profile < TOML < environment < explicit CLI.
- Define secret indirection, redacted provenance, unknown-key behavior, and
  atomic validation before bind or application import.
- Preserve the programmatic `Config` API or document a migration.

**Accept when:** sample static/WSGI/ASGI/ACME/manual-TLS configurations validate
without a runtime parser dependency and equivalent CLI/TOML inputs agree.

**Evidence (2026-07-11):**
[`production-edge-configuration.md`](production-edge-configuration.md) defines
the seven typed sections, exact overlay and repeatable-value semantics, secret
references/redaction/provenance, strict unknown-key handling, side-effect-free
atomic validation, and the compatibility path through the frozen public
`Config`. Five checked-in TOML examples cover static, WSGI, ASGI, ACME, and
password-protected manual TLS, with an explicit CLI-equivalence ledger. All five
parse with stdlib `tomllib`; `git diff --check` and strict MkDocs pass. Runtime
loading, `--check-config`, and installed-wheel behavior remain `EDGE-041` rather
than being implied by this design acceptance.

### [x] EDGE-004 — observability vocabulary and baseline

**Owner:** operations agent. **Depends on:** `EDGE-001`.

- Define bounded-cardinality request, connection, byte, queue, worker, reload,
  TLS/ACME, and rejection metrics.
- Define lifecycle events and stable failure/rejection reason codes.
- Forbid paths, query strings, client identities, domains, and exception text as
  metric labels; specify a low-cost disabled path.
- Retain exact current benchmark/build identities as the pre-edge baseline.

**Accept when:** later runtime tasks can instrument behavior without inventing
incompatible schemas.

**Evidence (2026-07-11):**
[Production-edge observability vocabulary](production-edge-observability.md)
defines bounded metric labels, lifecycle events, stable reason codes, disabled-
path cost policy, process aggregation semantics, and the retained exact-build
baseline. Endpoint and registry implementation remains `EDGE-040`.

## Wave 1 — lifecycle and process architecture

### [x] EDGE-010 — active-work registry and complete single-process drain

**Owner:** lifecycle agent. **Depends on:** `EDGE-001`, `EDGE-004`.

- Register active threaded sockets, ASGI connection/tasks, streams, WebSockets,
  HTTP/2 streams, and post-response/background work.
- Drain in order: stop admission, refuse new keep-alive/H2 work, notify
  cooperative protocols, wait, force-close at `drain_timeout`, then run ASGI
  lifespan shutdown.
- Add H2 `GOAWAY(NO_ERROR)` behavior that completes accepted streams.
- Make forced and unfinished work observable by type and reason.

**Accept when:** deterministic tests cover idle keep-alive, uploads, large/slow
responses, ASGI background and streaming work, WebSocket close, H2 GOAWAY,
deadline expiry, repeated shutdown, and leak-free cleanup. CPU-stuck WSGI is
documented as requiring supervisor termination.

**Evidence (2026-07-11):** the accepted
[ASGI graceful-drain](performance-experiments/2026-07-11-asgi-graceful-drain.md)
and
[threaded HTTP/1 and HTTP/2 drain](performance-experiments/2026-07-11-threaded-http-drain.md)
records cover the implementation and tradeoffs. Focused deterministic tests
cover idle and admitted work, large/slow responses, uploads, streaming and
post-response application work, WebSocket `1012`, H2 GOAWAY/refusal/completion,
forced deadlines, observable forced work, idempotence, bounded-worker rejection,
and exact-once capacity release. Cancellation-resistant ASGI and CPU-stuck WSGI
remain explicitly contained by the future worker supervisor rather than an
unbounded in-process wait.

### [x] EDGE-011 — parent-owned/pre-bound listener seam

**Owner:** runtime agent. **Depends on:** `EDGE-001`.

- Separate socket bind/listen from threaded and ASGI runtime construction.
- Adopt validated inherited descriptors without rebinding or double-close.
- Define parent/worker ownership for TCP plus singleton UDP/H3, TFTP, and mDNS.
- Compare descriptor passing with `SO_REUSEPORT`; record platform/fairness
  consequences before selecting a default.

**Accept when:** independent generations serve through the intended listener,
bind failures occur before worker launch, ephemeral ports report correctly, and
direct library startup remains supported.

**Evidence (2026-07-11):** the accepted
[parent-owned listener seam](performance-experiments/2026-07-11-parent-owned-listener.md)
adds a stdlib bind/scan primitive and validated descriptor-duplicate adoption
for threaded and ASGI runtimes. Tests prove caller/runtime lifetime independence,
two sequential generations on one parent listener, rejection without stealing
an invalid socket, retained ephemeral/scan behavior, worker-local TLS wrapping,
and ASGI lifespan cleanup on validation failure. The seam intentionally stops
before process transfer, readiness, or supervisor policy.

### [x] EDGE-012 — standard-library worker supervisor

**Owner:** supervisor agent. **Depends on:** `EDGE-010`, `EDGE-011`.

- Add `workers = 1|N|auto`; retain one process outside production defaults.
- Launch spawn-compatible workers, transfer listeners, collect startup status,
  and terminate the whole tree to a deadline.
- Require application import, listener setup, and ASGI lifespan completion before
  readiness. Set correct WSGI multiprocess metadata.
- Keep the control API portable even where Unix signals trigger it.

**Accept when:** 1/2/4 workers serve one port, a startup failure never reports
ready, Ctrl-C leaves no child, and per-worker memory/startup/scaling evidence is
recorded on GIL and free-threaded Python.

**Accepted (2026-07-11):** the
[fixed-generation supervisor](performance-experiments/2026-07-11-worker-supervisor.md)
now provides spawn-safe socket transfer, a no-admission
`PREPARED -> COMMIT -> READY` barrier, bounded drain/terminate/kill cleanup,
static/WSGI/ASGI startup readiness, correct WSGI process metadata, and one
parent-materialized TLS identity. Focused 1/2/4-worker, rollback, admission,
TLS, and cleanup tests pass on GIL and free-threaded CPython 3.14. The item
is closed with seven-trial controlled GIL and free-threaded artifacts covering
startup, summed process-tree RSS, CPU, errors, dispersion, latency, and scaling.
ASGI reached 3.94x GIL and 3.82x free-threaded throughput at four workers; the
record explicitly leaves production defaults, PSS, GIL static four-worker
efficiency, and free-threaded static overload to `EDGE-020`, `EDGE-021`, and
`EDGE-064`.

### [ ] EDGE-013 — worker recovery, recycling, and singleton ownership

**Owner:** supervisor/state agent. **Depends on:** `EDGE-012`, `EDGE-004`.

- Add crash detection, bounded exponential restart, crash-loop failure, worker
  generations, and request-count/age/jitter recycling.
- Elect parent/singleton ownership for access-log aggregation, ACME, mDNS, TFTP,
  and initial H3 operation.
- Define compression/digest caches as explicit per-worker budgets.
- Initially reject unsafe multi-worker write/WebDAV combinations; then design
  stdlib coordination for target leases, DAV locks, and global upload budgets.

**Accept when:** crash/hang/startup/recycling tests remain bounded; simultaneous
writes serialize across workers before the restriction is lifted; a dead worker
cannot strand a permanent lease or singleton.

**Checkpoint (2026-07-11):** the
[state-ownership contract](performance-experiments/2026-07-11-worker-state-ownership.md)
now makes connection, blocking-thread, compression-cache, digest-flight, and
application-state multiplication explicit. Config preflight continues to reject
uploads/DAV and unowned access-log, ACME, mDNS/QR, TFTP, H3, CGI, and proxy
combinations. It also defines the bounded parent lease/budget/singleton broker
required before those restrictions can be lifted. A recovery prototype failed
its cross-runtime teardown gate and was removed; recovery, recycling, and proven
cross-worker write/singleton coordination remain open acceptance gates.

### [ ] EDGE-014 — generation-based zero-downtime reload

**Owner:** lifecycle/supervisor agent. **Depends on:** `EDGE-003`, `EDGE-013`.

- Validate the complete candidate, start a new generation, wait for readiness,
  switch admission, and drain the old generation.
- Keep the old generation on invalid config or failed application startup.
- Provide a command/control API; Unix signals may be aliases, not the only path.

**Accept when:** repeated valid and invalid reloads under HTTP/1, H2, streaming,
and WebSocket traffic meet the availability objective, never expose a
half-applied configuration, and kill stale workers at the deadline.

## Wave 2 — scalable connection and blocking work

### [x] EDGE-020 — production selector decision

**Owner:** protocol/performance agent. **Depends on:** `EDGE-001`; reuse the
existing selector experiments.

- Decide `connection_backend = threaded|selector` using idle connections, TLS,
  parsing, pipelining, backpressure, cancellation, and failures—not static RPS
  alone.
- Keep the threaded path until the selector has semantic parity and a material
  production-shaped advantage across required platforms.

**Accept when:** the architecture record either selects a default or retains an
explicit experimental mode with missing gates; no benchmark prototype is called
production.

**Checkpoint (2026-07-11):** accepted. The
[connection backend decision](performance-experiments/2026-07-11-connection-backend-decision.md)
keeps threaded HTTP/1 as the only production backend and the selector under its
explicit comparison-harness name as a benchmark-only candidate. The record
weighs churn and tail-latency gains against warm-filesystem offload cost,
buffering/streaming memory tradeoffs, and missing TLS, request-body, dynamic,
WebSocket, proxy, and platform gates. It adds no public backend setting and
routes bounded blocking work and semantic parity to `EDGE-021` and `EDGE-022`.

### [ ] EDGE-021 — bounded blocking-work scheduler

**Owner:** runtime/performance agent. **Depends on:** `EDGE-020`.

- Create explicit bounded pools/queues for filesystem work, listings,
  compression, digests, archives/uploads, WSGI, and other blocking calls.
- Preserve evidence-based size/transport choices: bounded small reads inline,
  streaming/sendfile for large plaintext, and TLS-aware chunking.
- Reject before expensive work when saturated and prevent large work from
  starving cheap requests.

**Accept when:** mixed workloads have bounded memory/queues and continued cheap-
request progress while protected small/large-response benchmarks stay in budget.

**Checkpoint (2026-07-11):** in progress. The
[bounded-work scheduler record](performance-experiments/2026-07-11-bounded-work-scheduler.md)
inventories production and async blocking paths, preserves configurable
read/buffer/streaming choices, and defines the backend-neutral scheduler and
mixed-load gates. `src/servery/_work.py` now supplies exact nonblocking pool
capacity, byte-weight admission, cancellation/late-result ownership, snapshots,
shutdown, stable overload reasons, and physically separate filesystem/compute/
stream/application lanes. Focused unit tests pass, including cheap-lane progress
under compute saturation. Production classification, accurate byte charging,
integrations, and decision-grade mixed benchmarks remain open, so this task is
not accepted yet. The load generator now supports synchronized, separately
reported cohorts plus per-second completion counts; comparison-runner plumbing
and controlled mixed trials remain. The first production consumer is the
[bounded access-log handoff](performance-experiments/2026-07-11-production-access-log-scheduler.md):
record and byte budgets, block/drop policy, batching, failure accounting, and
finite drain are implemented and tested. Seven-trial evidence retains
synchronous logging as the default because lossless async logging missed the p99
budget; lossy mode remains explicit. Dynamic/H2/H3 commit hooks remain open.
HTTP/1 archive and selection streaming also support an optional per-worker
`max_archive_streams` inline lease acquired before headers. Saturation returns a
retryable `503`, cheap static work continues, and recovery is tested. Archive
mixed-load evidence and a production-profile default remain open.

### [ ] EDGE-022 — selector protocol/application parity

**Owner:** integration agent. **Depends on:** `EDGE-010`, `EDGE-020`, `EDGE-021`.

- Reach exact static HTTP/1 behavior through shared request/response planning.
- Integrate TLS, WSGI, ASGI HTTP, WebSockets, and proxying without duplicating
  security policy.
- Decide separately whether H2 remains synchronous or gains async I/O; do not
  hide that work inside selector promotion.
- Reject unsupported feature/backend combinations at configuration time.

**Accept when:** differential wire, disconnect, drain, slow-reader, and TLS tests
pass for both backends with a portable fallback and no duplicate path/parser.

## Wave 3 — production safety and operations

### [ ] EDGE-030 — cohesive resource policy and admission controls

**Owner:** resource-policy agent. **Depends on:** `EDGE-003`, `EDGE-004`,
`EDGE-013`.

- Unify existing connection/head/body/write/request-count controls without
  collapsing their distinct semantics.
- Add bounded header bytes/count, global and per-client token buckets, bounded
  client-state tables, queued-work and expensive-operation budgets.
- Wire equivalent policy to threaded HTTP/1, ASGI, H2, H3, and WebSockets.
- Use `429` for client rates and `503` for global saturation where a response is
  safe; emit stable reason metrics.
- Keep LAN behavior generous and make production defaults evidence-based.

**Accept when:** fake-clock, boundary, concurrent/free-threaded, recovery,
cross-transport, disabled-cost, and saturation tests demonstrate bounded state.

### [ ] EDGE-031 — protocol-specific abuse and fairness controls

**Owner:** protocol-security agent. **Depends on:** `EDGE-002`, `EDGE-030`.

- Bound H2/H3 concurrent streams, rapid resets, frame/control rates, header
  tables, buffered data, WebSocket messages, and compression expansion.
- Add optional global/client/transfer bandwidth ceilings and ensure large
  transfers cannot starve small work.
- Close only the abusive stream/connection where correctness permits.

**Accept when:** attack-shaped replays remain within CPU/memory limits, healthy
clients progress, and disabled/generous policies retain ordinary performance.

### [ ] EDGE-032 — trusted client identity

**Owner:** security/protocol agent. **Depends on:** `EDGE-002`, `EDGE-003`.

- Keep the socket peer authoritative by default.
- Add optional trusted CIDRs and strict `Forwarded`/`X-Forwarded-*` parsing for
  intentional trusted-hop deployments without requiring such a hop.
- Use resolved identity consistently in limits, logs, and ASGI scopes.

**Accept when:** spoofed/malformed/mixed-chain IPv4/IPv6 tests prove untrusted
clients cannot alter identity or scheme; direct edge remains the default.

### [ ] EDGE-040 — metrics, health, request context, and logs

**Owner:** operations agent. **Depends on:** `EDGE-004`, `EDGE-012`.

- Implement stdlib counters, gauges, fixed histograms, and parent aggregation.
- Serve `/livez`, `/readyz`, and Prometheus text on a separate loopback or Unix
  administration listener; public bind requires explicit protection.
- Make readiness reflect application startup, worker quorum, drain, and terminal
  certificate state.
- Add uniform request IDs and structured events across H1, H2, H3, and ASGI.
- Keep log handoff bounded with observable `block|drop|stderr` policy and safe
  rotation/reopen; redact secrets and query data by default.

**Accept when:** cardinality, concurrency, free-threaded, redaction, saturation,
and lifecycle-state tests pass with a measured disabled-path cost.

### [ ] EDGE-041 — TOML, overlays, secrets, and config checking

**Owner:** configuration agent. **Depends on:** `EDGE-003`.

- Implement stdlib `tomllib`, documented precedence, `--check-config`, and
  redacted effective configuration with provenance.
- Reject unknown fields and invalid combinations before bind or app import.
- Support protected secret files/environment references; retain command-line
  secrets only for compatibility with an explicit warning.

**Accept when:** precedence, redaction, permission, invalid-config, stable exit-
code, and installed-wheel CLI tests pass without a parser dependency.

### [ ] EDGE-042 — direct-edge service hardening

**Owner:** deployment agent. **Depends on:** `EDGE-011`, `EDGE-041`.

- Add Unix sockets and systemd socket adoption as integrations, not requirements.
- Add requested user/group drop, restrictive umask/state-directory policy, and
  fail-closed unsupported controls.
- Supply hardened systemd and non-root container examples plus read-only-root
  guidance. Remove `sudo servery` as production advice.

**Accept when:** Linux tests cover descriptor adoption, privilege drop, restart,
and state paths; the simple standalone command remains supported.

## Wave 4 — unattended TLS

### [ ] EDGE-050 — production crypto and certificate-store decision

**Owner:** TLS/security agent. **Depends on:** `EDGE-002`, `EDGE-003`.

- Decide whether to retain and independently review the pure-Python RSA/X.509
  path or spend the optional package budget on audited crypto. Record dependency,
  native-surface, timing, and maintenance consequences.
- Base renewal on parsed `notAfter` and SANs, not cache mtime.
- Validate matching cert/key pairs and use private, fsynced, atomic replacement
  with crash and Windows behavior documented.

**Accept when:** malformed/mismatched/expired/corrupt/interrupted state tests pass
and the security/dependency choice is explicit rather than accidental.

### [ ] EDGE-051 — continuous ACME scheduler and challenge service

**Owner:** ACME agent. **Depends on:** `EDGE-050`, `EDGE-004`, `EDGE-013`.

- Schedule by expiry with configurable renewal window, jitter, exponential
  backoff, and one elected renewal owner per certificate set.
- Keep valid material active through transient failure and serve HTTP-01
  challenges alongside the long-running edge server on port 80.
- Use fake clocks and a mock/pinned local CA for success, failure, restart,
  near-expiry, expiry, rate-limit, and shutdown cases.

**Accept when:** unattended multi-worker renewal never duplicates issuance,
busy-loops, or leaves partial state and exposes next-attempt/failure status.

### [ ] EDGE-052 — atomic live TLS activation

**Owner:** TLS/runtime agent. **Depends on:** `EDGE-014`, `EDGE-050`, `EDGE-051`.

- Atomically switch validated contexts for new handshakes while existing HTTP,
  streams, and WebSockets retain the old context.
- Roll back invalid candidates and coordinate H1/H2 ALPN plus an explicit H3
  restart/reconfiguration path.
- Tie certificate expiry and renewal state into readiness without taking down a
  still-valid service after one failed attempt.

**Accept when:** sustained handshakes and traffic across repeated valid/invalid
rotations observe only complete identities and no availability gap.

## Wave 5 — assurance and release

### [ ] EDGE-060 — distribution and package-budget verifier

**Owner:** packaging agent. **Depends on:** may start immediately.

- Smoke the wheel with `--no-deps` outside the checkout; rebuild the sdist and
  smoke its wheel the same way.
- Assert `py3-none-any`, no native binary payload, empty core dependency closure,
  correct metadata, and a separately reported H3 transitive closure.
- Exercise real static, TLS, H2, and ASGI requests against the installed product.

**Accept when:** an executable script enforces the exact deployment promise.

### [ ] EDGE-061 — pinned protocol and framework interop

**Owner:** interoperability agent. **Depends on:** may scaffold immediately.

- Pin curl/httpx, h2spec/nghttp2, TLS scanner, Autobahn WebSocket, Pebble ACME,
  Starlette, and FastAPI cohorts by version/image digest.
- Resolve or explicitly time-limit every known protocol failure; optional H3 is
  isolated from the core cohort.
- Cover SSE, multipart, trailers, background work, disconnects, lifespan,
  WebSocket close/fanout, and slow consumers.

**Accept when:** supported behavior is exact and unsupported extensions fail
explicitly; no unexplained skip or perpetual waiver remains.

### [ ] EDGE-062 — fuzzing and corpus CI

**Owner:** fuzz/security-test agent. **Depends on:** `EDGE-002`.

- Build harnesses/corpora for HTTP/1, HPACK/H2, WebSocket, multipart, WebDAV,
  archive, ACME, and configuration parsing.
- Run deterministic corpus mutation on pull requests and coverage-guided jobs on
  a schedule; persist every crash/hang as a regression case.
- Assert bounded time/allocation, no path escape/partial write, and deterministic
  close/reset behavior in addition to no exceptions.

**Accept when:** every public parser has a bounded harness, corpus, owner, and
reproducer workflow; unresolved crashing inputs block release.

### [ ] EDGE-063 — lifecycle failure injection and soak

**Owner:** reliability agent. **Depends on:** `EDGE-014`, `EDGE-040`, `EDGE-052`.

- Inject worker exits/hangs, invalid reloads, bind failure, disk full/read-only,
  descriptor pressure, slow/dropped clients, log saturation, clock movement,
  renewal failure, and shutdown during traffic.
- Run mixed static/ASGI/WebSocket load through reload, recycling, renewal, and
  overload while measuring RSS, descriptors, tasks, threads, processes, queues,
  errors, and recovery time.

**Accept when:** critical scenarios leave no orphan process/socket/artifact, the
release soak has no unexplained errors or sustained resource slope, and forced
cleanup is counted.

### [ ] EDGE-064 — controlled performance and scaling gates

**Owner:** performance agent. **Depends on:** `EDGE-012`, `EDGE-021`, `EDGE-030`.

- Gate memory-per-connection, multi-worker scaling, small/large static, dynamic
  wait, streaming, TLS, and H2 on controlled hardware.
- Validate 100/1,000/10,000 connections with a load generator and network setup
  proven not to be the bottleneck. Timed-error or client-saturated runs are not
  rankable.
- Retain raw results with exact source, image, Python, kernel, and CPU identities.

**Accept when:** resource and regression budgets pass and the 10,000-connection
result is valid rather than merely attempted.

### [ ] EDGE-065 — exact-tag CI and release gate

**Owner:** workflow integrator only. **Depends on:** `EDGE-060..064`.

- Make release publication call the same exact-tag functional matrix as pull
  requests plus artifact, package, protocol, and production-lifecycle gates.
- Add scheduled fuzz/interop/soak jobs and explicit skip/waiver accounting.
- Never publish from the current build-and-metadata-only release path.

**Accept when:** only artifacts produced from and tested at the release tag can
reach the package index.

### [ ] EDGE-070 — production profile and release candidate

**Owner:** coordinator/release agent. **Depends on:** all P0 production tasks and
`EDGE-060..065`.

- Assemble evidence-based finite defaults without silently changing LAN/dev.
- Require trusted TLS for public production unless a high-friction development
  override is explicit; reject dangerous anonymous-write, CGI, TFTP, self-signed,
  and public-admin combinations by default.
- Provide minimal static and ASGI configurations plus deploy, inspect, reload,
  rollback, renew, backup, and shutdown instructions.
- Execute the full release checklist against built artifacts on a fresh host.

**Accept when:** an operator can deploy and maintain a directly exposed HTTPS
service using Python, servery, a domain or certificate, and one configuration,
with no hidden external edge or process manager.

## Four-slot agent execution model

With one coordinator and three subagents, use non-overlapping lanes:

| Lane | Ownership | First batch |
| --- | --- | --- |
| Coordinator | product contract, integration, conflict review, gates | `EDGE-001` |
| Agent A | lifecycle, listener, supervisor | `EDGE-010`, then `EDGE-011` |
| Agent B | selector, offload, performance | `EDGE-020`, then `EDGE-021` |
| Agent C | security, configuration, operations | `EDGE-002` and `EDGE-004` |

The second batch is `EDGE-003`, `EDGE-011`, `EDGE-021`, and `EDGE-060/062`
scaffolding. Begin the supervisor only after drain and listener ownership are
accepted. Only one agent edits `server.py`, `config.py`, or a protocol state
machine at a time. Only the workflow integrator edits `.github/workflows/`.

## Verification floor

Start focused, then run all applicable project gates before accepting a task:

```console
make check
python -m unittest
python -m coverage run -m unittest
python -m coverage report
ruff check src tests scripts
ruff format --check src tests scripts
python scripts/check_zero_deps.py
python -m build
uv run --group docs mkdocs build --strict
```

Also run changed-platform, free-threaded, installed-wheel, interop, security,
failure, soak, and controlled-performance tiers as applicable. A required gate
that was unavailable is reported as not run, never passed.

## Explicitly deferred

- General upstream pools and load-balancer algorithms.
- Automatic retry of arbitrary proxied requests.
- General memory/disk reverse-proxy caching.
- Arbitrary multi-site routing or configuration languages.
- Distributed multi-host coordination.
- Required external metrics, state, or secret services.
- HTTP/3 as a condition of the first production claim.

None of these is allowed to block a sound directly exposed single-service server.

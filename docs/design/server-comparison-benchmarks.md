# External server comparison benchmarks

Status: implemented 2026-07-10. The executable harness is
`scripts/compare_servers.py`; this document defines what its numbers do and do
not mean.

## What is being compared

There is no honest single ranking for servery, nginx, Caddy, Gunicorn, and
Uvicorn. nginx and Caddy are edge/static servers, Gunicorn is a WSGI process
manager, Uvicorn is an ASGI server, and servery spans smaller versions of all
three roles. The suite therefore reports three separate cohorts:

| Cohort | Implementations | Shared work |
| --- | --- | --- |
| Static | servery, nginx, Caddy | Return the same file bytes from the same read-only corpus |
| WSGI | servery WSGI, Gunicorn `gthread` | Run the same WSGI callable from the same Python image |
| ASGI | servery ASGI, Uvicorn | Run the same ASGI callable from the same Python image |

Never combine the three cohorts into one winner. A static-server result says
nothing about application-server semantics, and comparing a WSGI blocking wait
directly with an ASGI nonblocking wait would compare interfaces rather than
servers.

## Workload matrix

The default run measures one-connection latency and 64-connection throughput.
Large-transfer and connection-churn cases have lower concurrency caps to keep the
client and loopback stack from becoming the experiment.

| Scenario | Cohort | Response | Connection behavior | Purpose |
| --- | --- | ---: | --- | --- |
| `static-1k` | Static | 1 KiB | Keep-alive | Cached small-asset/request overhead |
| `static-8m` | Static | 8 MiB | Keep-alive, cap 16 | Large-file throughput and memory |
| `static-churn-1k` | Static | 1 KiB | New TCP connection/request, cap 32 | Accept/close and thread/task setup cost |
| `static-range-64k` | Static | 1 KiB `206` from a 64 KiB file | Keep-alive, opt-in | Range selection and partial-body transfer |
| `static-not-modified` | Static | Bodyless `304` | Keep-alive, opt-in | Conditional selection without body transfer |
| `static-index-1k` | Static | 1 KiB `index.html` | Keep-alive, opt-in | Directory lookup, index selection, and file response |
| `static-download-1k` | Static capability subset | 1 KiB attachment | Keep-alive, opt-in | Query parsing and exact disposition policy |
| `static-spa-1k` | Static capability subset | 1 KiB root index for a missing route | Keep-alive, opt-in, SPA enabled | Fallback lookup and configured routing overhead |
| `static-gzip-cache-64k` | Static capability subset | Deterministic coded body | Keep-alive, opt-in, 32 MiB retained cache | Warm encoded-representation hits |
| `static-gzip-miss-64k` | Static capability subset | Deterministic coded body | Keep-alive, opt-in, zero retained cache | Sustained compression and transient same-key sharing |
| `static-digest-miss-64k` | Static capability subset | 64 KiB identity plus exact `Repr-Digest` | Keep-alive, opt-in, zero retained entries | Sustained hashing, opened identity, and transient same-key sharing |
| `static-listing-100` | Static capability subset | Exact 56.8 KiB generated page | 64 keep-alive clients, opt-in | Bounded 100-entry scan/render and worker policy |
| `static-listing-1000` | Static capability subset | Exact 519.8 KiB generated page | 16 keep-alive clients, opt-in | Heavier scan/stat/render and worker scaling |
| `static-access-log-1k` | Static capability subset | 1 KiB plus one CLF record | 64 keep-alive clients, opt-in | Logged/unlogged overhead, bounded selector overflow policy, and record delivery |
| `wsgi-1k` | WSGI | 1 KiB | Keep-alive | Minimal synchronous application overhead |
| `wsgi-body-64k` | WSGI | 64 KiB request / 1 KiB response | Keep-alive, cap 32, opt-in | Synchronous body consumption and deadline cost |
| `wsgi-wait-10ms` | WSGI | 1 KiB after blocking 10 ms | Keep-alive | Blocking-I/O concurrency and thread scheduling |
| `wsgi-stream-64k` / `wsgi-stream-1m` | WSGI | 16 generator chunks | Keep-alive, opt-in | Synchronous streaming and client/body throughput |
| `asgi-1k` | ASGI | 1 KiB | Keep-alive | Minimal asynchronous application overhead |
| `asgi-body-64k` | ASGI | 64 KiB request / 1 KiB response | Keep-alive, cap 32, opt-in | Async body-event consumption and deadline cost |
| `asgi-churn-1k` | ASGI | 1 KiB | New TCP connection/request, cap 32, opt-in | Protocol/task setup and teardown cost |
| `asgi-headers-32` | ASGI | 1 KiB | Keep-alive, 32 request fields, opt-in | Parser cost as field count grows |
| `asgi-wait-10ms` | ASGI | 1 KiB after nonblocking 10 ms | Keep-alive | Async concurrency and scheduler overhead |
| `asgi-stream-64k` / `asgi-stream-1m` | ASGI | 16 body events | Keep-alive, opt-in | Async streaming, backpressure, and body throughput |
| `asgi-slow-reader-64m` | ASGI | 1,024 distinct 64 KiB events | Four throttled keep-alive readers, opt-in | Producer-ahead buffering and cgroup peak memory |
| `asgi-starlette-json` | ASGI | Small JSON | Keep-alive, opt-in | Real Starlette routing/request/JSON behavior on CPython 3.15 |
| `asgi-starlette-stream-64k` | ASGI | 16 framework body events | Keep-alive, cap 32, opt-in | Starlette producer/disconnect task-group behavior |
| `asgi-fastapi-json` / `asgi-fastapi-validation` | ASGI | Small JSON / exact `422` | Keep-alive, opt-in | FastAPI/Pydantic validation on the current compatible CPython 3.14 image |

The wait is a controlled scheduling probe, not an application benchmark. Real
applications add framework, database, serialization, and network costs which can
dwarf the server layer.

Capability-subset scenarios record their permitted adapters and required launch
policy. They exclude an otherwise eligible static server when its configured
semantics differ; byte equality alone is not enough. Probe-declared response
headers are validated exactly before timing.

## Fairness contract

Every timed comparison applies these controls:

1. **Common protocol:** plaintext HTTP/1.1 over Linux host networking. Automatic
   HTTPS, HTTP/2, HTTP/3, and response compression are disabled or avoided. Those
   need separate protocol cohorts, not a feature-rich configuration on only one
   server.
2. **Correctness before speed:** before timing, the harness checks expected
   status, body length, and SHA-256. Timed unexpected responses, failed reads, or
   failed connects are errors; any error invalidates the run and produces a
   nonzero exit. Opt-in scenarios may provide validated custom request fields and
   expect RFC-bodyless 1xx/204/304 responses without `Content-Length`.
3. **Same resources:** each server gets the same Docker CPU set. The client is
   pinned to a disjoint CPU set. Static servers mount the same corpus; all Python
   servers use the same comparison image and interpreter.
4. **Same client:** one load generator, request framing, warmup, duration,
   connection count, and process count are used for every implementation in a
   cohort. It understands an orderly `Connection: close` and reconnects without
   misreporting it as a server failure.
5. **Warm steady state:** an untimed workload-specific warmup precedes every
   sample. This intentionally measures warm page-cache operation. Server order is
   rotated deterministically across trials to reduce order and thermal bias.
6. **Reproducible identity:** JSON evidence records host details, the Python GIL
   state, source revision/dirty state, harness file hashes, image references and
   IDs, a hash of the complete Python product tree, exact scenario hashes,
   controls, raw samples, and medians. For published
   results, pass immutable `image@sha256:...` references as well. Summary rows
   retain min/max and median absolute deviation (MAD), rather than hiding trial
   spread behind a single median.
7. **No hidden scale-out:** the default is one server CPU and one application
   worker. An explicit scale run gives every server the same CPU set and gives
   nginx, Caddy, Gunicorn, and Uvicorn their available worker controls. servery
   remains one process because it does not currently ship a process supervisor;
   that is a product capability difference, not something the benchmark should
   conceal.
8. **No hidden log loss:** the access-log cohort drains and truncates warmup
   records before timing, then counts timed lines after the writer quiesces.
   Delivery percentage is reported beside throughput. The cgroup memory snapshot
   occurs before the out-of-band audit helper starts inside the container.

The official static images necessarily use their own base distributions. This
measures the deployable artifacts operators actually run. The dynamic cohort is
stricter: servery, Gunicorn, and Uvicorn are installed in one image, so their OS
and CPython build are identical.

Dynamic fixtures are bind-mounted read-only from the current harness into every
dynamic container, including a prebuilt servery baseline image. This is required
for paired source decisions: otherwise an older image could run an older embedded
application while the candidate runs a new endpoint, invalidating the comparison.
The fixture file hash and product-image identities remain recorded separately.

Gunicorn uses `gthread` with total threads matched to the requested connection
count so the WSGI wait scenario is not serialized by configuration. The Uvicorn
baseline explicitly uses `--loop asyncio --http h11`; it does not rely on
Uvicorn's environment-sensitive `auto` detection. The opt-in
`uvicorn-native` adapter explicitly uses `--loop uvloop --http httptools` in a
labeled compatible Python 3.14 image. Native packages remain comparison-only
dependencies and are never installed in servery's runtime.

High-connection experiments must also name their client shape. Separate-run
warmup measures a fresh connection burst. `--persistent-warmup` preserves
established connections into the timed interval, while `--connection-ramp`
stages their creation. `--max-latency-samples` bounds only retained latency
observations; request/byte/error counts remain exact. The current host cannot
rank 10,000 loopback clients without transport errors, so those artifacts are
capacity diagnostics rather than benchmark results.

## Running it

Docker, Linux host networking, and at least two available CPUs are required.

```bash
# Inspect the matrix without pulling or building images.
uv run python scripts/compare_servers.py --list

# Fast end-to-end smoke of all three cohorts.
uv run python scripts/compare_servers.py \
  --scenario static-1k --scenario wsgi-1k --scenario asgi-1k \
  --concurrency 8 --warmup 0.25 --duration 1 --trials 1

# Standard baseline: concurrency 1 and 64, three trials, one server CPU.
uv run python scripts/compare_servers.py \
  --json benchmarks/artifacts/external-comparison.json

# Publication run with a longer steady state and more repetitions.
uv run python scripts/compare_servers.py \
  --warmup 3 --duration 15 --trials 7 \
  --concurrency 1 --concurrency 16 --concurrency 64 --concurrency 256 \
  --client-procs 4 \
  --json benchmarks/artifacts/external-comparison-publish.json

# Explicit four-CPU scale test. Choose CPU IDs valid on the host.
uv run python scripts/compare_servers.py \
  --server-cpus 0-3 --client-cpus 4-7 --app-workers 4 --client-procs 4 \
  --json benchmarks/artifacts/external-comparison-scale.json
```

The current defaults are declared at the top of the harness: CPython
`3.15.0b3-slim`, nginx `1.29.8-alpine`, Caddy `2.11-alpine`, Gunicorn `26.0.0`,
Uvicorn `0.51.0`, and Starlette `1.3.1`. FastAPI `0.139.0` is an explicit
`--include-fastapi` layer; currently pair it with `python:3.14.3-slim` because
its Pydantic Core dependency has no cp315 wheel. They are inputs, not claims that
these versions remain the right comparison forever:

```bash
uv run python scripts/compare_servers.py \
  --python-image python:3.15.0b3-slim \
  --nginx-image nginx:1.29.8-alpine \
  --caddy-image caddy:2.11-alpine

# Current FastAPI/Pydantic compatibility cohort.
uv run python scripts/compare_servers.py \
  --python-image python:3.14.3-slim --include-fastapi \
  --scenario asgi-fastapi-json --scenario asgi-fastapi-validation \
  --server servery-asgi --server uvicorn
```

Use `--no-pull` and `--no-build` only when deliberately reusing local images.
The JSON image IDs make that choice visible. A free-threaded CPython result is a
separate runtime cohort: supply a compatible free-threaded base image through
`--python-image`, label it separately, and do not average it with the normal-GIL
run.

For candidate decisions, `--servery-baseline-image TAG` adds the old image to
every selected servery cohort: `servery-baseline`, `servery-wsgi-baseline`, and
`servery-asgi-baseline`. The paired summary matches each candidate to its cohort
baseline and reports the median and dispersion of within-trial RPS and p99
changes. This avoids comparing time-separated aggregate medians and permits a
single implementation change to be checked against protected static and dynamic
paths.

The `Performance trends` GitHub Actions workflow also exposes an opt-in
`external_comparison` manual input and retains its JSON artifact. It is not part
of the weekly run or a pull-request gate: shared hosted-runner noise and mutable
external products make it evidence for inspection, not a stable servery
regression baseline.

## Reading the evidence

The headline fields are requests/second, payload MB/second, p50/p90/p95/p99
latency, cgroup peak memory, errors, and load-generator CPU consumption.

- Compare medians only at the same scenario and connection count. Concurrency 1
  is useful for request overhead; higher concurrency is useful for saturation
  throughput and tail behavior. Inspect min/max and MAD before treating a small
  difference as real; add trials when the spread overlaps the claimed gain.
- Treat client CPU near 90–100% as a warning that the load generator may be the
  bottleneck. Summary rows mark this condition with `client_limited: true` and an
  asterisk in terminal output. Add client processes and isolated client CPUs,
  then confirm that the server result stops increasing before publishing it.
- `memory.peak` includes the runtime and cgroup-accounted page cache where the
  host exposes cgroup v2. It is useful for like-for-like footprint comparisons,
  not as a language heap measurement. An unavailable value is reported as null,
  not zero.
- The slow-reader scenario consumes 16 KiB every 1 ms and intentionally runs
  past its nominal duration until each connection completes its first response.
  Read its cgroup peak and error count first. Its request rate and roughly
  five-second p99 reflect client pacing, not maximum server throughput.
- The 8 MiB result is usually page-cache and loopback limited. It tests the
  server's transfer path; it does not predict spinning-disk, network, TLS, or WAN
  throughput.
- A 10 ms wait should approach a workload-imposed ceiling. The useful signals
  are whether concurrency is maintained, how much scheduler overhead remains,
  and what happens to p99—not who makes a deliberately sleeping app “fast.”

## Deliberate omissions and next tiers

This first external suite does not claim to measure:

- cold filesystem cache (portable cache eviction would require privilege and can
  disturb the host);
- TLS handshakes, resumed TLS, HTTP/2 multiplexing, or HTTP/3/QUIC;
- on-the-fly gzip, Brotli, or Zstandard, whose availability and policies differ;
- directory listings, error pages, uploads, WebDAV, or proxying, where response
  semantics are not byte-identical between products;
- multi-host network behavior, packet loss, remote constrained links, or
  long-lived WebSockets (the suite has only a controlled loopback slow reader);
- framework/database application performance.

Those are good second-tier experiments, but each needs its own shared semantics,
validation, and client. Adding them to one “requests per second” chart would make
the comparison broader and less fair at the same time.

The [Performance and production-gap research roadmap](performance-gap-research-roadmap.md)
uses this suite's evidence to sequence profiling, low-risk static experiments,
connection-architecture spikes, dynamic validation, and production-origin work.

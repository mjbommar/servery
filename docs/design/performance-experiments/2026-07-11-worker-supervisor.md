# Fixed-generation worker supervisor

Date: 2026-07-11
Backlog item: `EDGE-012`

## Decision

Servery uses a standard-library `multiprocessing` supervisor when `workers > 1`.
The parent binds and retains the one TCP listener and passes the socket object to
spawn-context workers. Passing the object, rather than an integer descriptor,
lets `multiprocessing` use its supported socket reducers on Windows and POSIX.
Each runtime adopts its own duplicate; TLS wrapping is worker-local and cannot
mutate the parent's listener.

Startup is an explicit barrier:

1. Every worker imports and constructs its static/WSGI runtime, or imports its
   ASGI application, completes lifespan startup, and constructs an asyncio
   server with `start_serving=False`.
2. Each worker reports `PREPARED` while admitting no requests.
3. The parent waits for the complete prepared quorum, then broadcasts `COMMIT`.
4. Workers start accepting and acknowledge `READY`; only a complete ready quorum
   makes `Supervisor.start()` succeed.

This prevents an early worker from responding while a later worker can still
fail application import or ASGI lifespan startup. WSGI receives
`wsgi.multiprocess=True` and `wsgi.multithread=True`.

Shutdown first closes the parent's listener and signals every worker to use the
existing runtime drain path. At `drain_timeout`, survivors receive termination;
at `force_timeout`, remaining survivors receive kill. POSIX workers create a
session so forced signals cover descendants as well as the direct worker.
Windows uses the portable direct-process termination boundary. Cleanup reports
an error rather than claiming success if a process survives the final deadline.
Normal portable control uses an Event; SIGTERM and Ctrl-C are front ends, not
the control API. Each worker also owns only the receive end of a one-way
`multiprocessing.Pipe`, while the supervisor owns its send end and never writes
application data. Pipe EOF is therefore a portable supervisor-liveness signal:
a worker waiting at `PREPARED` cancels startup, and a worker at `READY` enters
the same runtime drain path used by a normal stop. This avoids Linux-only parent
death signals and raw descriptor assumptions while ensuring a hard parent exit
does not leave worker listener duplicates serving indefinitely. There is no
restart authority after parent loss; workers only drain and exit.

Once a generation is reaped, the supervisor closes every status and liveness
connection and process handle and releases its references to the Event
synchronization objects rather than retaining kernel resources until garbage
collection of the Supervisor. A cancellation-resistant application can still
outlive a lost parent because no supervisor remains to enforce the terminate
and kill deadlines; service managers and container runtimes should retain a
whole-process-tree kill boundary for that failure case.

## Deliberate scope

The first generation supports read-only static serving, WSGI, and ASGI. Config
validation rejects write/upload/WebDAV, CGI, proxy, HTTP/3, TFTP, mDNS, QR,
ACME, and file access-log combinations for multiple workers until singleton or
shared-state ownership is designed. TLS certificate files and parent-created
self-signed material are supported. Self-signed material is generated once by
the parent, so all workers present the same identity.

There is intentionally no crash restart, crash-loop policy, recycling, reload,
or singleton election here; those are `EDGE-013` and `EDGE-014`.

## Resource semantics

Current limits and caches are per worker. With `N` workers:

- `max_connections` permits up to `N * max_connections` aggregate connections;
- compression and digest caches can consume up to `N` times their configured
  per-worker budgets;
- `max_workers` creates that many blocking threads in each worker;
- application globals and ASGI lifespan state are process-local.

These semantics are explicit until parent aggregation and global budgets land
under `EDGE-013`. Operators should size each value as a per-process budget.

## Verification

Focused tests cover the direct one-worker runtime, two and four workers sharing
one port, WSGI metadata, the ASGI admission barrier, ASGI startup-failure
rollback, invalid WSGI-import rollback, cancellation during the prepared
barrier, simulated control EOF both before commit and after readiness, bounded
force-kill of a cancellation-resistant worker, status/control handle cleanup,
idempotent child reaping, SIGTERM and Ctrl-C control semantics, and one TLS
identity across repeated multi-worker connections. The EOF tests close the
supervisor's real write ends rather than manufacturing descriptor numbers, so
they exercise the same spawn-compatible `Connection` ownership used in
production. The broader ASGI suite covers the gated API's unchanged
single-process behavior.

Commands and measured GIL/free-threaded results are recorded with the backlog
checkpoint after both interpreters complete.

Functional results on this host:

| Runtime | Command | Result |
| --- | --- | --- |
| CPython 3.14.3, GIL | `uv run --python 3.14 python -m unittest tests.test_supervisor -v` | 17 passed in 9.806 s |
| CPython 3.14.3, free-threaded | `uv run --python 3.14t python -m unittest tests.test_supervisor` | 17 passed in 9.051 s |
| CPython 3.14.3, GIL | `uv run --python 3.14 python -m unittest tests.test_supervisor tests.test_wsgi tests.test_smoke -v` | 53 passed in 17.234 s |

Ruff, formatting, the relevant `ty` scope, and `git diff --check` also pass.

## Controlled scaling experiment

The acceptance run used HTTP/1.1 plaintext over Docker host networking on
Linux 6.18.0, with server workers pinned to distinct physical cores
(`0`, `0,2`, or `0,2,4,6`) and eight client CPUs kept separate. Every point is
the median of seven 10-second trials after a three-second persistent-connection
warmup. The workload was a 1 KiB warm-cache static response or a 1 KiB ASGI
response. Worker count and available server cores increased together, so these
results measure process-and-core scaling; they do not isolate process overhead
from added CPU capacity.

The GIL series used CPython 3.15.0b3 (`gil=True`). The free-threaded series used
CPython 3.14.3 (`gil=False`). The GIL series and free-threaded ASGI series used
concurrency 256. A free-threaded static concurrency-256 baseline overloaded the
thread-per-connection runtime on one CPU and produced 1,839 errors, so the
rankable static free-threaded series was rerun at concurrency 64. This is
intentionally not a full runtime-by-workload factorial and is not evidence for
a production default.

### Throughput and tail latency

`Efficiency` is throughput scaling divided by worker count, relative to the
one-worker point in the same runtime/workload series. Dispersion is median
absolute deviation (MAD); all rankable points had zero errors.

| Runtime / workload | Workers / cores | Median RPS | Scaling / efficiency | RPS MAD | Median p99 | p99 MAD | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3.15.0b3 GIL / static, c256 | 1 / 1 | 18,142 | 1.000x / 100.0% | 0.27% | 115.413 ms | 1.273 ms | 0 |
| 3.15.0b3 GIL / static, c256 | 2 / 2 | 34,097 | 1.879x / 94.0% | 4.00% | 44.819 ms | 1.597 ms | 0 |
| 3.15.0b3 GIL / static, c256 | 4 / 4 | 23,331 | 1.286x / 32.1% | 0.35% | 28.767 ms | 0.132 ms | 0 |
| 3.15.0b3 GIL / ASGI, c256 | 1 / 1 | 36,381 | 1.000x / 100.0% | 0.27% | 8.161 ms | 0.072 ms | 0 |
| 3.15.0b3 GIL / ASGI, c256 | 2 / 2 | 71,673 | 1.970x / 98.5% | 0.50% | 4.261 ms | 0.005 ms | 0 |
| 3.15.0b3 GIL / ASGI, c256 | 4 / 4 | 143,380 | 3.941x / 98.5% | 0.24% | 2.391 ms | 0.087 ms | 0 |
| 3.14.3 free-threaded / static, c64 | 1 / 1 | 21,203 | 1.000x / 100.0% | 0.28% | 1.152 ms | 0.006 ms | 0 |
| 3.14.3 free-threaded / static, c64 | 2 / 2 | 41,194 | 1.943x / 97.1% | 1.54% | 1.252 ms | 0.164 ms | 0 |
| 3.14.3 free-threaded / static, c64 | 4 / 4 | 76,992 | 3.631x / 90.8% | 1.91% | 27.879 ms | 5.544 ms | 0 |
| 3.14.3 free-threaded / ASGI, c256 | 1 / 1 | 38,557 | 1.000x / 100.0% | 0.36% | 7.922 ms | 0.038 ms | 0 |
| 3.14.3 free-threaded / ASGI, c256 | 2 / 2 | 74,472 | 1.931x / 96.6% | 2.87% | 6.293 ms | 1.854 ms | 0 |
| 3.14.3 free-threaded / ASGI, c256 | 4 / 4 | 147,079 | 3.815x / 95.4% | 4.70% | 3.433 ms | 1.071 ms | 0 |

The strongest result is ASGI: four workers delivered 3.94x GIL and 3.82x
free-threaded throughput with 95--99% scaling efficiency. The free-threaded
static c64 series also reached 3.63x, although its four-worker p99 increase
shows that throughput alone is not a sufficient default-selection criterion.
GIL static serving improved substantially at two workers but collapsed in
efficiency at four despite using 3.74 CPU cores. That unresolved path belongs
to `EDGE-020`, `EDGE-021`, and the controlled matrix in `EDGE-064`.

The failed free-threaded static c256 point is not ranked: 256 simultaneously
runnable free-threaded handlers on one CPU could not sustain that offered
concurrency. It is evidence for the
global scheduler and overload work in `EDGE-021`, not evidence that
free-threading or the supervisor intrinsically fails to scale.

### Startup, CPU, and memory

Startup is the median time from container start until the benchmark readiness
probe succeeded. `Ready RSS` sums RSS across the observed process tree; it can
double-count shared pages. PSS was unavailable in every artifact, so no claim
about unique proportional memory is possible. CPU is the median average server
cores used over each observation window.

| Runtime / workload | Workers | Container start | Ready | Ready RSS | CPU cores | Container peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GIL / static | 1 | 182.0 ms | 596.2 ms | 36.2 MiB | 0.97 | 47.1 MiB |
| GIL / static | 2 | 181.5 ms | 1,016.8 ms | 127.1 MiB | 1.95 | 108.5 MiB |
| GIL / static | 4 | 183.5 ms | 1,021.3 ms | 200.4 MiB | 3.74 | 157.4 MiB |
| GIL / ASGI | 1 | 184.3 ms | 598.5 ms | 35.8 MiB | 0.96 | 29.0 MiB |
| GIL / ASGI | 2 | 180.7 ms | 1,021.6 ms | 126.7 MiB | 1.94 | 89.9 MiB |
| GIL / ASGI | 4 | 182.4 ms | 1,021.3 ms | 199.8 MiB | 3.86 | 139.8 MiB |
| Free-threaded / static | 1 | 179.3 ms | 551.9 ms | 46.6 MiB | 0.97 | 45.4 MiB |
| Free-threaded / static | 2 | 181.8 ms | 989.8 ms | 168.7 MiB | 1.95 | 157.2 MiB |
| Free-threaded / static | 4 | 183.1 ms | 1,045.2 ms | 261.7 MiB | 3.90 | 228.1 MiB |
| Free-threaded / ASGI | 1 | 180.1 ms | 589.1 ms | 46.2 MiB | 0.96 | 35.9 MiB |
| Free-threaded / ASGI | 2 | 182.9 ms | 1,041.3 ms | 168.8 MiB | 1.89 | 123.3 MiB |
| Free-threaded / ASGI | 4 | 185.6 ms | 1,019.9 ms | 259.5 MiB | 3.83 | 193.2 MiB |

The supervisor barrier adds roughly 0.4--0.5 seconds versus direct one-worker
readiness, but readiness is effectively flat from two to four workers in these
runs. From two to four workers, summed ready RSS increased by about 36.6 MiB per
additional GIL worker and 45--47 MiB per additional free-threaded worker. The
one-to-two delta also includes the parent, multiprocessing resource tracker,
and the transition from the direct path, so it must not be described as one
worker's marginal memory. Container peaks provide a second bounded observation,
not a substitute for unavailable PSS.

### ASGI `TCP_NODELAY` correction

The first exploratory multi-worker ASGI runs stalled near 6.2k RPS because the
asyncio path left small writes exposed to the Nagle/delayed-ACK interaction.
Setting `TCP_NODELAY` on accepted ASGI transport sockets removed that artifact;
the final GIL ASGI series then scaled from 36.4k to 143.4k RPS. A focused test
now guards the socket option. This was a transport correctness/performance fix,
not a supervisor scaling optimization.

### Reproducibility and artifacts

Both final images contain source commit
`5076e4e68f0e9283e9ac801c69e65115c2cc8f46` from a dirty tree whose product
tree SHA-256 is
`457ab6092241e33224ba01fba65dac54401e6131d6e3ea6d8a5829943053b033`.
The exact images were:

- CPython 3.15.0b3, GIL enabled:
  `servery-edge012-gil-final:20260711`, image
  `sha256:2a1fff5fb9a789b9ab22a9b521bc8973def81873f57e0e8a86b225d177ef90c9`.
- CPython 3.14.3, GIL disabled:
  `servery-edge012-ft-final:20260711`, image
  `sha256:9732a6134f2b21366f81cf6015cff197cb4f53eebeb2afbaadbc7b939261f1f3`.

Shared harness hashes were `compare_servers.py`
`05f880a1534ded0f9e9b3ef7d2c6b811024cf72ef9afc7473345a81b19426ad3`,
the comparison Dockerfile
`c7b621c757c3b43213c78826a8a1c57c6dba9926b68eb1cca853782fa43d937b`,
`apps.py` `c85ca00ad2eb43c773ef92d681a9e9c557461a66c43a2f8625d607bc41d09ad2`,
`starlette_apps.py`
`1e3d9c15cfb95f888d928aa11a2dc96a0cc6ebe6196c7a8b2ee810274bebc891`,
and `fastapi_apps.py`
`1262281158e3fa28a7384a7502ae1095cdd2a15eb291bd17f7920b6e627696d1`.
`loadgen.py` was
`12f21b4be2a7b07a583f13ef375c1474cb3f5cff85a438c2cf4eff118848af28`
for the GIL, one-worker free-threaded, and free-threaded static artifacts, and
`77421cf3847a0a9e318975d6d1ab566109b69447a3018977be70eab91f4b8fd3`
for the final free-threaded ASGI two/four-worker artifacts. The free-threaded
ASGI scaling series therefore crosses a recorded load-generator revision even
though its declared controls and result schema are unchanged; that is an
explicit reproducibility caveat and another reason not to treat this as a full
production-default matrix.

Raw final artifacts:

- GIL: `benchmarks/artifacts/edge012-final-gil-w1-c1.json`,
  `benchmarks/artifacts/edge012-final-gil-w2-c2.json`, and
  `benchmarks/artifacts/edge012-final-gil-w4-c4.json`.
- Free-threaded ASGI: `benchmarks/artifacts/edge012-final-ft-w1-c1.json`,
  `benchmarks/artifacts/edge012-final-ft-asgi-w2-c2.json`, and
  `benchmarks/artifacts/edge012-final-ft-asgi-w4-c4.json`.
- Free-threaded static c64:
  `benchmarks/artifacts/edge012-final-ft-static-c64-w1-c1.json`,
  `benchmarks/artifacts/edge012-final-ft-static-c64-w2-c2.json`, and
  `benchmarks/artifacts/edge012-final-ft-static-c64-w4-c4.json`.

The earlier `edge012-gil-w2-c2.json` used SMT sibling CPUs, while
`edge012-gil-w1-c1.json`, `edge012-gil-w2-c2-physical.json`, and
`edge012-gil-w4-c4-physical.json` used an earlier image without the final ASGI
`TCP_NODELAY` behavior. They are retained as exploratory diagnostics only and
are invalid for rankable scaling claims.

## Conclusion

`EDGE-012` meets its bounded acceptance criteria: one, two, and four workers
share one port; startup rollback and tree cleanup are tested; and controlled
GIL/free-threaded startup, CPU, memory, error, dispersion, latency, and scaling
evidence is retained. The evidence supports an explicit multi-worker option,
especially for ASGI, but does not establish `auto` or any multi-worker count as
a production default. Recovery/recycling, global resource budgets, the GIL
static four-worker collapse, free-threaded overload behavior, and the broader
production comparison remain follow-on work in `EDGE-013`, `EDGE-020`,
`EDGE-021`, and `EDGE-064`.

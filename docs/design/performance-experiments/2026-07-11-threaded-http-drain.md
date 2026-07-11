# Threaded HTTP/1 and HTTP/2 graceful drain — 2026-07-11

Status: accepted as the threaded-runtime portion of `EDGE-010`. Together with
the ASGI drain record, it closes the single-process HTTP/1, HTTP/2, ASGI, and
WebSocket drain milestone. Supervisor-level forced process termination remains
separate work.

## Boundary and policy

The threaded server now owns an explicit registry of admitted sockets. A
connection-limit permit is associated with the socket that acquired it, and
removing that ownership is the only operation that releases the permit. This
makes cleanup idempotent across normal completion, worker submission failure,
bounded-worker queue rejection, and forced shutdown.

`begin_draining()` establishes one fixed deadline and stops admission. HTTP/1
finishes an in-flight response with `Connection: close` and does not read
another keep-alive request. At the deadline, remaining sockets are shut down
and closed. Executor cleanup uses `wait=False, cancel_futures=True`: a stuck
application callable cannot extend graceful cleanup indefinitely, although a
future process supervisor remains responsible for terminating a CPU-stuck
worker.

Protocol notification hooks run on short-lived daemon threads. This is
deliberate: an H2 GOAWAY write can be flow- or kernel-buffer-blocked and must
not move unbounded socket I/O onto the shutdown caller. The fixed drain
deadline remains the upper bound and force-closes any notification or response
that cannot make progress.

## HTTP/2 behavior

On drain, an H2 connection sends `GOAWAY(NO_ERROR)` with the highest stream ID
accepted before the drain boundary. Stream admission and that last-stream
snapshot share a lock, so the rule remains atomic on free-threaded Python.
Later streams receive `RST_STREAM(REFUSED_STREAM)`, while accepted streams may
consume further flow-control credit and complete normally.

An idle H2 connection sends GOAWAY and then shuts down its read side. This
wakes a handler blocked in `read1()` without waiting for the full drain
deadline. Connections with accepted work keep reading because their responses
may still need client `WINDOW_UPDATE` frames.

## Deterministic evidence

Focused socket-level tests cover:

- bounded-worker saturation removes the socket from the registry;
- a max-connection permit is released exactly once, including duplicate
  cleanup;
- a blocking protocol notification does not block `begin_draining()`;
- deadline expiry force-closes an unfinished socket;
- executor shutdown is explicitly nonblocking;
- a large in-flight HTTP/1 response advertises `Connection: close` and completes
  during drain;
- an admitted multipart upload completes and commits during drain;
- repeated begin/close operations are idempotent;
- a forced deadline emits an observable typed warning;
- idle H2 emits GOAWAY and exits promptly;
- GOAWAY reports the accepted stream boundary;
- a post-boundary H2 stream is refused; and
- a flow-control-blocked stream accepted before drain completes successfully.

The original focused cohort and complete modules passed on CPython 3.14.3 and
free-threaded CPython 3.15.0a5. The integrated follow-up adds the large-response,
upload, idempotence, and forced-event assertions and reruns the complete modules
as part of the project gate.

## Remaining production lifecycle work

ASGI HTTP, WebSocket, streaming, and post-response application work are covered
by the companion ASGI registry rather than one artificial cross-thread/event-
loop container. Stable forced-drain reason codes are fixed in the production
observability vocabulary; counters and the administration endpoint remain
`EDGE-040`. The separate optional HTTP/3 listener needs generation/supervisor
integration before the broader production release, but is not part of the
accepted initial single-process drain boundary.

The forced-close policy cannot stop a CPU-stuck WSGI callable inside the same
process. `ThreadPoolExecutor.shutdown(wait=False)` keeps the cleanup call
bounded, but CPython retains worker threads until their callables return; the
production supervisor must terminate the worker process after its deadline.

## Disabled-path performance gate

The request path originally read the drain flag while holding the connection-
registry condition. The accepted implementation publishes draining through a
`threading.Event`, retaining the condition for registry/deadline mutation while
making ordinary `is_draining` checks lock-free and safe on free-threaded Python.

A rotated same-source Docker gate compared the event candidate with the
condition-read baseline on CPython 3.15.0b3, one server CPU, four client CPUs,
two client processes, 0.75 second warmup, seven three-second trials, and exact
1 KiB response validation. At one connection the paired median was -1.4% RPS /
+1.3% p99 with 7.8% / 2.7% MAD. At 64 connections it was +4.8% RPS / -2.3%
p99 with 5.6% / 6.4% MAD. All responses were correct with zero errors. Every
effect is smaller than its dispersion, so the disabled path is neutral rather
than credited with a speedup.

Raw artifact:
`benchmarks/artifacts/2026-07-11-drain-event-gate.json`. Candidate image
`sha256:f016f437d88f81d8213e2e0957f9dc6ea0a9d3f4c35966e25f9b09f91ea11936`;
baseline image
`sha256:76e1d72dd73faed44e8f2d290565fb9d00cecb8e2c43d6003821830d2ac98d2a`.

# ASGI graceful drain — 2026-07-11

Status: accepted for the single-process ASGI server.

## Question

Can servery stop accepting work, let admitted HTTP requests finish, notify
WebSockets of a service restart, enforce one operator-selected deadline, and
only then run application lifespan shutdown without adding a runtime package?

The important performance choice is not to poll every task or introduce a
queue on the request hot path. The server instead keeps one set entry per
accepted transport and changes a small connection phase marker at the request
admission boundary.

## Accepted behavior

The listener and application lifecycle now shut down in this order:

1. close the listening socket and set the process drain event;
2. close idle transports so a keep-alive connection cannot admit another
   request;
3. allow admitted HTTP application work to complete and send its response;
4. send accepted WebSockets close code `1012` (service restart) and expose a
   matching `websocket.disconnect` event to the application;
5. wait up to `drain_timeout` for tracked connection tasks;
6. cancel remaining tasks and abort their transports; and
7. send `lifespan.shutdown` while application resources are still valid.

`drain_timeout=0` is an immediate forced drain. A positive timeout is a single
total grace budget, not a fresh per-connection budget, so shutdown time does not
grow with connection count.

## Admission and tracking boundary

Each accepted transport has one registry record containing its callback task,
writer, and current phase. A connection is `idle` while waiting for a complete
request head, `http` once that head wins the admission race, and `websocket`
after a valid upgrade reaches the application.

This boundary deliberately permits a complete, already-admitted request to
finish after the stop signal. It prevents later keep-alive requests from
reaching the application. Closing idle writers also wakes head reads without
waiting for the ordinary keep-alive timeout.

Starlette/FastAPI response background callbacks execute inside the ASGI
application coroutine and are therefore covered by the tracked connection
task. Arbitrary tasks an application detaches with `asyncio.create_task()` are
not owned by the server and cannot be discovered reliably.

## Deadline tradeoff and containment limit

Python cancellation is cooperative. After the deadline, servery cancels each
remaining connection task, aborts its transport, and gives cancellation one
event-loop turn. It does not await a task indefinitely if application code
catches and suppresses `CancelledError`; doing that would make
`drain_timeout` advisory rather than bounded.

Such a cancellation-resistant task can remain alive inside the process after
`serve_forever()` returns. Hard containment requires the planned worker
supervisor to terminate a worker process after its drain budget. The current
single-process implementation guarantees the network deadline and lifecycle
ordering for cooperative applications, but does not claim to preempt arbitrary
Python code.

## Deterministic experiment

The focused `GracefulDrainTest` cohort uses real loopback sockets and controlled
events rather than timing-dependent sleeps. It proves:

- an admitted response completes before lifespan shutdown;
- an idle keep-alive connection closes when draining starts;
- an application blocked past the deadline is cancelled, its transport is
  aborted, and lifespan shutdown follows;
- streaming response work and post-response application work finish before
  lifespan shutdown;
- a cancellation-resistant task cannot extend the configured deadline; and
- an accepted WebSocket receives wire close code `1012`, while the ASGI app
  receives disconnect code `1012`.

Command:

```bash
.venv/bin/python -m unittest tests.test_asgi.GracefulDrainTest -v
```

Result: six focused tests pass, and the full 77-test ASGI cohort passes on the
managed project interpreter. Forced deadline and cancellation-suppression paths
also emit stable warning events until the metrics registry in `EDGE-040` lands.

## Decision

Keep registry and drain coordination in the ASGI listener, because it owns the
accepted task and writer lifetimes. Keep the timeout in shared configuration so
the later supervisor can use the same operator budget. Do not hide forced
termination behind a longer hard-coded wait: application shutdown needs vary,
and a process supervisor is the correct mechanism for code that refuses
cooperative cancellation.

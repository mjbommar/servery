# Response write-progress timeout — 2026-07-10

Status: accepted as an opt-in cross-transport policy. It remains disabled by
default and no profile selects a value.

## Problem and semantics

Synchronous HTTP/1, WSGI, proxy, and HTTP/2 writes inherited the active socket
`--timeout`, but asyncio ASGI/WebSocket drains and HTTP/3 flow-control waits could
remain blocked indefinitely. ASGI streaming backpressure bounded memory, but one
stalled client could still retain its admitted connection and application task.

`--write-timeout SECONDS` now bounds one wait without response-write progress:

- threaded HTTP/1 headers, WSGI/proxy/CGI output, archive chunks, direct
  `sendall`, and `sendfile` use a scoped socket timeout for each write;
- HTTP/2 applies the same scope to SETTINGS, control, HEADERS, and DATA frames;
- ASGI HTTP and WebSocket drains receive an asyncio deadline;
- HTTP/3 aborts a stream if its bounded sender queue remains above the capacity
  threshold for the configured interval.

The deadline resets after a successful write or drain. It is not a total response
wall-clock limit or a bandwidth floor: a slow client that continues to make
progress may keep a long transfer active. Those policies require separate
observable rate and total-phase budgets.

The default is `None`, meaning no *separate* write-progress policy. This preserves
existing behavior: synchronous sockets still have the general `--timeout`, while
async drains retain their native transport behavior. A universal inherited
30-second asyncio deadline was rejected because it charged every response for a
timer even when the operator had not requested the policy.

On an ASGI timeout, servery aborts the transport rather than asking
`StreamWriter.close()` to flush bytes already queued to the stalled peer. The
application's `send()` raises `TimeoutError` (an `OSError` subclass), `finally`
blocks execute, and the admitted slot is released. Gracefully flushing after a
deadline would defeat the resource-release guarantee.

## Implementation and performance iterations

The default hot path directly calls the original `writer.drain()`; it does not
enter a shared coroutine. When the option is enabled, servery checks the public
transport low-water threshold first. At or below that threshold, drain cannot
remain paused for buffer relief, so no timer is allocated. Above it,
`asyncio.timeout()` bounds the wait.

CPython 3.15.0b3, one server CPU, 64 connections, two client processes, rotated
paired trials produced these decision signals:

| Shape | ASGI 1 KiB RPS | p99 | Decision |
| --- | ---: | ---: | --- |
| disabled policy through an extra drain coroutine | -6.3% | +8.2% | reject |
| disabled policy with original direct drain | -0.4% | +1.0% | accept |
| configured `wait_for()` on every drain | -15.7% | +15.6% | reject |
| configured `asyncio.timeout()` on every drain | -12.9% | +6.6% | revise |
| configured low-water-aware timer, same image A/B | +2.1% | +1.6% | accept as neutral |

The final current-tree same-image control used nine four-second trials. RPS-ratio
MAD was 3.5%; p99-ratio MAD was 2.9%, both wider than the measured movement, so
neither change is distinguishable from host noise. Static 1 KiB default-path
control measured +2.6% RPS / -4.4% p99 with
wide dispersion and no errors. The accepted conclusion is that the disabled path
is neutral and the explicitly enabled policy stays within the protected
throughput budget; it is not a speed improvement.

## Behavioral evidence

Unit tests cover scoped synchronous timeout restoration, immediate async drains,
deadline expiry, and cancellation. A real ASGI socket test sets a 50 ms write
deadline, requests a one-gigabyte allocated stream, deliberately does not read,
and verifies that the application's `finally` block runs and the connection is
aborted. Normal static, ASGI, WebSocket, HTTP/2, and HTTP/3 suites protect the
non-stalled paths.

Ignored evidence under `benchmarks/artifacts/`:

- `write-timeout-default-paired-2026-07-10.json` (rejected helper shape);
- `write-timeout-default-v2-paired-2026-07-10.json` (accepted disabled path);
- `write-timeout-enabled-paired-2026-07-10.json` (`wait_for` rejection);
- `write-timeout-enabled-v3-paired-2026-07-10.json` (unconditional timer revision);
- `write-timeout-enabled-v4-paired-2026-07-10.json` (buffer-aware diagnostic);
- `write-timeout-config-cost-v4-paired-2026-07-10.json` (pre-final same-image diagnostic);
- `write-timeout-config-cost-v5-paired-2026-07-10.json` (accepted current-tree policy cost).

## Remaining work

- Measure a constrained remote link and TLS, not only loopback plaintext.
- Add an explicit total production request-head deadline. Total HTTP/1 body
  consumption is now a separate opt-in policy; the active socket timeout still
  resets whenever an operation succeeds.
- Design bandwidth/fairness policy separately from stall detection.
- Expose timeout/abort counters once bounded-cardinality metrics exist.

# ASGI 2.4 closed-send errors — 2026-07-11

Status: accepted as the completion of the advertised ASGI HTTP 2.4 lifecycle
boundary.

## Gap and semantics

servery advertised `scope["asgi"]["spec_version"] == "2.4"`, but a `send()`
after the final response event could write again, and some peer write failures
escaped as ordinary application exceptions. The
[ASGI HTTP specification](https://asgi.readthedocs.io/en/latest/specs/www.html#disconnected-client-send-exception)
says a send on a closed connection should raise a server-specific `OSError`
subclass and that the server must not log the resulting uncaught exception as an
application fault.

The accepted policy distinguishes:

- **completed request scope:** `_body_complete is None`, the terminal marker
  already introduced for post-response `receive()`;
- **closed transport:** `StreamWriter.is_closing()` before a send;
- **native peer write failure:** non-timeout `OSError` from header/body writes or
  intermediate drains; and
- **server write-progress timeout:** `TimeoutError`, which retains the existing
  server-owned abort path rather than being mislabeled as peer disconnect.

The first three raise `ClientDisconnectError(OSError)` to the application. If
the application catches it, cleanup proceeds normally. If it does not, the
exchange consumes the expected lifecycle exception without an error log. A
fully framed response can still leave the physical HTTP/1 connection reusable;
an uncaught extra send therefore does not discard a valid pipelined next request.
Actual peer loss naturally reaches EOF on the next parser iteration.

This is not configurable. The exception type is an advertised interface rule,
not operator resource policy.

## Correctness evidence

Tests cover:

- a send after a final response raises `ClientDisconnectError`, which is an
  `OSError` subclass;
- applications can catch that exception;
- an uncaught exception produces no `ASGI app error` log and two pipelined
  requests still receive complete responses;
- an already-closing writer raises before accepting response state;
- native `BrokenPipeError` is normalized to the server-specific exception; and
- blocked-drain write timeouts remain `TimeoutError`.

Existing real/synthetic receive-disconnect, response framing, streaming,
backpressure, keep-alive, TLS, and pipeline tests protect the surrounding
lifecycle.

## Performance gate

Artifacts:

- `benchmarks/artifacts/closed-send-final-2026-07-11.json`;
- diagnostic `benchmarks/artifacts/closed-send-v1-quick.json`.

The final five-trial gate uses CPython 3.15.0b3 with the GIL, one isolated server
CPU, disjoint client CPUs, balanced order, one-second warmup, five-second trials,
correct response probes, and zero timed errors.

| Scenario | Connections | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|---:|
| ASGI keep-alive 1 KiB | 64 | -3.36% | 3.15 points | +1.26% | 2.59 points |
| ASGI churn 1 KiB | 32 | -2.93% | 2.09 points | +0.73% | 1.07 points |

Both throughput points stay inside the protected 5% budget and are comparable
to dispersion; p99 and peak memory are neutral. The preceding three-trial probe
was +7.5% keep-alive and +1.0% churn, so no directional performance claim is
warranted.

The exact image is `sha256:f983522a...` with product-tree hash `4104854d...`.

## Decision and follow-ups

Accept the boundary and retain ASGI HTTP spec version 2.4. The remaining ASGI
work is broader compatibility and feature coverage rather than a known mismatch
in the core HTTP request/response/disconnect lifecycle:

- Starlette/FastAPI/Uvicorn application cohorts;
- response-message ordering/validation and trailers;
- server-shutdown and TLS half-close races; and
- HTTP/2-to-ASGI if that adapter is added.

## Verification

- 822 functional tests pass on CPython 3.15 with the GIL and 822 pass on its
  free-threaded build; both populated environments have four optional skips.
- Focused ASGI lifecycle tests pass on CPython 3.14.
- Repository-wide Ruff lint/format, `git diff --check`, Bandit, ty, and strict
  MkDocs pass.
- Fresh wheel and source distributions build; the wheel has no unconditional
  runtime dependencies and passes import/CLI smoke outside the source tree.

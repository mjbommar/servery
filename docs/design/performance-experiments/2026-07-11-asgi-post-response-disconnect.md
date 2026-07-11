# ASGI post-response disconnect — 2026-07-11

Status: accepted as a non-configurable ASGI request-scope correctness rule.

## Gap

The accepted lazy peer observer reports real EOF without consuming pipelined
bytes, but a reusable connection stayed pending if an application called
`receive()` after completing its response. The
[ASGI HTTP specification](https://asgi.readthedocs.io/en/latest/specs/www.html#disconnect-receive-event)
requires that call to return `http.disconnect`: the event closes the one-request
ASGI scope, not necessarily the underlying persistent HTTP/1 socket.

This distinction matters for framework task groups. A response producer may
finish while a sibling is waiting for disconnect; the waiter must be released
so the application scope can end, while the connection parser must retain all
bytes belonging to the next request.

## Accepted design

Response completion reuses `_ResponseState._body_complete`, whose callable was
already consulted before headers are committed:

1. `_AsyncBody` itself replaces the old per-request lambda and is callable for
   the same request-body-complete check.
2. A completed response changes that existing slot to `None`, the request-scope
   terminal marker.
3. If no application is waiting, the final body event pays one assignment and
   one exact-type branch; no future, event, task, set, or callback object exists.
4. A terminal `receive()` made after completion returns `http.disconnect`
   immediately.
5. A terminal `receive()` made earlier lazily replaces the slot with one
   `_ResponseCompletion` subscriber set and races its future against the
   existing real-peer future. Response completion or actual EOF wakes it.
6. Multiple listeners share the subscription. Cancelling the last listener
   restores the original body-complete callable if the response is still open.

The synthetic event does not close the TCP/TLS connection. After the application
returns, the normal ASGI exchange can parse the next buffered/pipelined head.
No operator switch is appropriate: this is interface semantics, unlike timeout
values and resource limits.

## Correctness evidence

Tests prove that:

- a response followed by `receive()` gets immediate `http.disconnect`;
- two pipelined requests both receive responses even though each request scope
  gets its synthetic terminal event;
- multiple already-pending terminal receives wake together on response
  completion;
- a receive before any response still waits for real peer EOF;
- cancellation restores both response and protocol subscriptions and preserves
  a pipelined request; and
- plaintext/TLS real EOF and truncated request-body disconnect behavior remain
  unchanged.

## Performance exploration

The first shape retained a `bind_response()` Python call on every request and a
generic subclass check on every final body. Its five-trial gate measured -4.07%
RPS/+1.19% p99 for 64-connection keep-alive and -7.15%/-0.39% for 32-connection
churn. The latter exceeded the point budget, so the shape was not accepted.

The final shape assigns the internal state directly and uses an exact-type check.
The short diagnostic then reversed direction (+0.7% keep-alive and +7.1% churn),
showing that host variance dominated changes this small. The decision therefore
uses the longer final gate and a concurrency-one churn resolution rather than
the favorable probe.

## Final gates

Artifacts:

- `benchmarks/artifacts/post-response-disconnect-final-2026-07-11.json`;
- `benchmarks/artifacts/post-response-disconnect-c1-final-2026-07-11.json`;
- diagnostics: `post-response-disconnect-v1-final-2026-07-11.json` and
  `post-response-disconnect-v2-quick.json`.

All use CPython 3.15.0b3 with the GIL, one isolated server CPU, disjoint client
CPUs, balanced order, correct response probes, and zero timed errors.

| Scenario | Connections | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|---:|
| ASGI keep-alive 1 KiB | 64 | -2.25% | 8.02 points | -0.44% | 1.48 points |
| ASGI churn 1 KiB | 32 | -10.16% | 9.29 points | -1.17% | 2.48 points |
| ASGI churn 1 KiB | 1 | +8.79% | 7.45 points | +2.88% | 6.32 points |

Keep-alive is neutral: throughput dispersion is much wider than the point
estimate and p99 is flat. The 32-connection churn point is not a defensible
regression claim because its ratio MAD is nearly the whole effect, its range
crosses zero, the immediately preceding diagnostic was +7.1%, and the
concurrency-one resolution is favorable with similarly wide dispersion. Churn
p99 and peak memory are neutral in every run. This evidence supports a
correctness fix, not a performance claim.

The final image is `sha256:af904690...` with product-tree hash `7308fa35...`.

## Decision and follow-ups

Accept lazy synthetic response completion alongside lazy real-peer observation.
The two causes share the event type but retain separate ownership: one ends the
request scope; the other also indicates transport loss.

The closed-send follow-up is now completed by
[ASGI 2.4 closed-send errors](2026-07-11-asgi-closed-send.md). Still open:

- Starlette/FastAPI/Uvicorn compatibility cohorts;
- server-shutdown and TLS half-close races; and
- HTTP/2-to-ASGI if that adapter is added.

## Verification

- 818 functional tests pass on CPython 3.15 with the GIL and 818 pass on its
  free-threaded build; both populated environments have four optional skips.
- Repository-wide Ruff lint/format, `git diff --check`, Bandit, and ty pass.
- Strict MkDocs, fresh wheel/sdist, and installed-wheel smoke are final release
  gates for the combined roadmap checkpoint.

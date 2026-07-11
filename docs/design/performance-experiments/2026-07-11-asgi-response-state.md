# ASGI response ordering, framing, and trailers — 2026-07-11

Status: accepted as a non-configurable ASGI/HTTP correctness boundary. HTTP/1
trailer transmission remains request-negotiated rather than operator-enabled.

## Gap and standards boundary

The ASGI adapter previously accepted only the happy-path
`http.response.start`/`http.response.body` sequence. It did not enforce the full
event state machine, exact `Content-Length`, application return before response
completion, or the ASGI HTTP trailers extension.

The [ASGI HTTP specification](https://asgi.readthedocs.io/en/latest/specs/www.html)
requires response start before response body and makes a response with
`trailers=True` incomplete until the application sends a terminal trailers
event. The
[HTTP trailers extension](https://asgi.readthedocs.io/en/latest/extensions.html#http-trailers)
is advertised in `scope["extensions"]`; on HTTP/1 the server transmits the
fields only when the request includes `TE: trailers`. RFC 9110 section 6.5 also
forbids generating trailer fields whose definitions do not permit trailers and
warns that routing, framing, authentication, response-control, and content-format
fields generally need to be known before content.

The accepted response state machine therefore:

- rejects body before start, duplicate start, body after its final event,
  trailers outside the declared trailer phase, unknown events, and sends after
  scope completion;
- delays committing response start until the first body event, preserving the
  ability to replace an uncommitted application failure or incomplete return
  with `500`;
- validates lower-case byte-native response fields and rejects control/newline
  injection;
- owns `Connection` and `Transfer-Encoding`, honors an app request to close
  without forwarding duplicate connection fields, and never emits both
  `Content-Length` and transfer coding;
- enforces exact `Content-Length` for ordinary body-bearing responses and closes
  an already-committed partial response on underflow or overflow;
- suppresses payload bytes and unnecessary close/chunk framing for `HEAD`, 1xx,
  `204`, and `304` responses;
- advertises `http.response.trailers`, supports multiple trailer events, uses
  chunked framing when the client negotiated trailers, and consumes but does not
  transmit trailer events when it did not; and
- rejects connection, routing, and framing fields such as `Content-Length`,
  `Transfer-Encoding`, `Connection`, `Host`, and `Upgrade` in a trailer block.

## Configuration decision

Ordering, field syntax, exact framing, and incomplete-response detection are
wire-safety and advertised-interface rules. They are not compatibility toggles.
Making them optional would create parser differentials and make the claimed ASGI
spec version depend on undocumented operator state.

Trailer *transmission* already has the right per-request control: the client
sends `TE: trailers`. An application that declares trailers still completes the
same ASGI event sequence when the client did not negotiate them, but servery
discards the fields after validation. This avoids silently changing application
lifecycle while protecting intermediaries that cannot preserve trailers. No
global `--trailers` switch is added.

## Streaming and hot-path design

The state machine does not buffer response bodies or trailer fields. Each
intermediate body/trailer event retains the existing drain/backpressure rule;
the terminal event uses the exchange's final drain. Negotiated HTTP/1 trailers
write the zero chunk, stream each validated field, and finish with the terminal
CRLF.

The ordinary benchmark response has two already-materialized fields
(`content-type`, `content-length`). A narrow validated tuple marker reuses their
serialized form and a known-present field set. Generic iterables, different
ordering, policies, connection fields, trailers, and malformed shapes stay on
the complete validator. The marker is representation-safe: application headers
are always materialized as a list, so an invalid list beginning with raw bytes
cannot masquerade as prevalidated internal data.

Several broader shapes were rejected or refined:

- the first complete state machine added separate booleans/counters and a
  generic response-header materialization pass;
- the second deferred start-header validation to the existing header writer;
- the third added the narrow common response-field path; and
- the accepted shape compacts trailer phase flags into one bit field, tracks
  remaining content length rather than expected-plus-sent counters, and handles
  an incomplete application return directly without a normal-path exception.

## Correctness evidence

Tests cover strict event ordering, unknown events, duplicate start, exact/short/
long content length, server-owned transfer and connection fields, malformed
outer header shapes, body suppression, negotiated and unnegotiated trailers,
multiple trailer events, forbidden framing trailers, incomplete application
return, two-response pipelines, and exact end-to-end trailer wire bytes. Existing
streaming drain, cancellation, write timeout, disconnect, TLS, request framing,
and closed-send tests protect the surrounding lifecycle.

## Performance gate

Artifacts:

- `benchmarks/artifacts/response-state-final-2026-07-11.json` (final gate);
- `response-state-v1-quick.json` through `response-state-v4-quick.json`
  plus `response-state-v7-final-rejected.json` (optimization diagnostics);
- `response-state-v9-minimal-c1.json` and `response-state-v9-stream-c1.json`
  (single-connection controls); and
- `response-state-v4-v3-stream-control.json` and
  `response-state-v9-v8-stream-control.json` (host-noise controls).

The final gate uses CPython 3.15.0b3 with the GIL, one isolated server CPU,
disjoint client CPUs, balanced order, one-second warmups, five-second trials,
correct response probes, and zero timed errors. Results are filled from the
completed exact-image artifact below.

| Scenario | Connections | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|---:|
| ASGI keep-alive 1 KiB | 64 | -13.75% | 2.21 points | +6.73% | 6.36 points |
| ASGI churn 1 KiB | 32 | +6.68% | 22.11 points | -7.36% | 6.86 points |
| ASGI stream 64 KiB/16 events | 32 | -1.02% | 13.10 points | +3.01% | 14.51 points |
| ASGI keep-alive 1 KiB | 1 | -7.20% | 9.69 points | +3.36% | 14.59 points |
| ASGI stream 64 KiB/16 events | 1 | -4.75% | 4.77 points | +3.93% | 4.10 points |

Streaming is neutral at 32 connections and remains inside the protected budget
at one. Churn is unresolved because dispersion is much wider than every point
estimate. Minimal keep-alive does **not** pass the saturated protected gate: the
final point is -13.75%, while a preceding exact-image three-trial probe was
+2.2% with 7.1-point MAD and concurrency one is -7.2% with 9.7-point MAD. This
is recorded as an unresolved capacity cost, not averaged into a neutral claim.

The discrepancy is larger than isolated state-machine cost. Three 300,000-
response in-container runs put v9's one-event median about 3% behind the frozen
baseline; the 16-event loop is about 21% behind because exact length/type/order
checks run per event. A direct five-trial v9-versus-v8 streaming control was
neutral (-1.1% RPS/-0.4% p99) with roughly 12-point dispersion. Earlier
miswired controls that selected `servery-comparison:local` instead of the named
candidate are retained as diagnostics but excluded from the decision.

The exact candidate is `sha256:1860cac4...`; its product-tree hash is
`28c8123e...`. The frozen closed-send baseline is `sha256:f983522a...`.

## Decision and remaining work

Accept the strict response state machine and HTTP trailers extension with a
documented minimal-response performance exception. Do not claim a gain from
hot-path specialization: it exists to contain the cost of mandatory correctness.
Ordering, framing, and field-injection safety do not become configurable merely
because the saturated point misses the 5% target. Keep the exact benchmark
artifact and rejected shapes so future cleanup cannot accidentally reintroduce
their cost, and rerun the minimal gate on a quieter/dedicated host before calling
the capacity question resolved.

Still open:

- Starlette/FastAPI compatibility and native Uvicorn differential cohorts;
- large response-header/cookie and high event-count scaling;
- lifespan/shutdown/TLS half-close races;
- field-registry-aware trailer policy beyond the explicit routing/framing deny
  set; and
- HTTP/2-to-ASGI only if that adapter is deliberately added.

## Verification

- 829 functional tests pass on CPython 3.15 with the GIL and 829 pass on its
  free-threaded build; both populated environments have four optional skips.
- All 61 focused ASGI tests pass on CPython 3.14.
- Repository-wide Ruff lint/format, `git diff --check`, Bandit, ty, and strict
  MkDocs pass.
- Fresh wheel and source distributions build. The wheel declares no
  unconditional runtime dependencies; wheel and source-distribution installs
  pass import/CLI smokes outside the source tree.

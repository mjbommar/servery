# HTTP/1 request-count connection policy — 2026-07-10

Status: accepted as a configurable resource policy; disabled by default. The
threaded/static, WSGI, and ASGI HTTP/1 paths close a connection after a configured
number of completed request heads. This experiment does not add worker-age or
wall-clock connection recycling, and it does not apply the count to HTTP/2 or
HTTP/3 streams.

## Decision

Add `max_requests_per_connection` to `Config` and
`--max-requests-per-connection N` to the CLI:

- `0` means unlimited and preserves the existing default behavior;
- a positive value makes that request the final one on the connection;
- the final response explicitly carries `Connection: close` and a pipelined
  request beyond the limit is not dispatched;
- negative values fail configuration;
- the network-facing `cdn` and `app` profiles use `1000`, while other profiles
  retain the unlimited default.

This setting is operator policy rather than an implementation tuning knob. A
finite count bounds how long connection-local state and one process generation
can serve the same client, but it adds reconnect and TLS-handshake work. The
general default therefore remains off. Profiles intended for longer-running
origin/application use select a generous count, and an operator can override it
in either direction.

The count is deliberately not a substitute for idle, header-read, body-read, or
write deadlines. Those bound elapsed time and slow-client occupancy; this policy
only bounds successful request reuse.

## Correctness contract

The threaded handler increments the count after a valid request head. When the
limit is reached it marks the connection non-persistent before dispatch and adds
`Connection: close` to the final response. Because persistence is server-owned,
an application-supplied Connection field is replaced with `close` on that
terminal response. This work runs only on the terminal request, so the unlimited
threaded path does not scan every response header.

ASGI owns the same count in its connection exchange. Its final response state is
non-persistent and serializes `Connection: close`; already-buffered bytes for a
later pipelined request are not dispatched. WSGI uses the threaded handler and
therefore shares its behavior.

Wire tests cover the explicit close field, one-response-only pipeline behavior,
and `0` serving two requests on one socket. Configuration, CLI, and profile tests
cover validation, propagation, defaults, and overrides.

## Performance method

The comparison harness was extended so an old prebuilt image can be paired with
the candidate in all three servery cohorts:

- `servery-baseline` for static;
- `servery-wsgi-baseline` for WSGI;
- `servery-asgi-baseline` for ASGI.

Candidate and baseline rotate within each trial. Acceptance uses the median of
per-trial ratios, not ratios of independently sampled aggregate medians. The final
run used CPython 3.15.0b3 with the GIL enabled, one server CPU, two client
processes, 64 connections, seven trials, 0.75 second warmup, and 3 second samples.
All timed samples had zero errors and the client was not saturated.

| Scenario | Throughput change | p99 change | Ratio MAD (RPS / p99) | Decision |
| --- | ---: | ---: | ---: | --- |
| static 1 KiB | +2.2% | +0.4% | 4.9% / 11.6% | neutral; noisy tail |
| WSGI 1 KiB | -0.8% | -1.0% | 3.2% / 6.8% | neutral |
| ASGI 1 KiB | -0.5% | -0.1% | 15.8% / 10.2% | neutral; noisy independent run |

The static and WSGI figures are from the final code after removing default-path
response-header tracking. The ASGI figure is from the preceding paired run after
restoring its original parser; the later threaded-only cleanup cannot affect it.
All changes are within the protected 5% throughput budget. These are neutrality
checks, not performance improvements.

Artifacts are retained outside git under `benchmarks/artifacts/`:

- `connection-policy-no-header-tracking-2026-07-10.json` is the accepted final
  static/WSGI decision;
- `connection-policy-final-paired-2026-07-10.json` supplies the restored-ASGI
  neutrality check but contains superseded static/WSGI code;
- `connection-policy-static-final-2026-07-10.json` and
  `connection-policy-wsgi-long-2026-07-10.json` are diagnostic intermediate runs.

## Rejected ASGI parser convergence

The same work tested whether ASGI should consume `RequestHeadParser`, which now
defines strict request-line, Host, field-syntax, framing, and persistence policy
for the threaded and selector adapters. Three adapters were tried: incremental
feeding, whole-block parsing, and a byte-native header adapter. Even the fastest
byte-native form regressed the minimal ASGI workload by 18.1% throughput at 64
connections and 19.4% at one connection; the one-connection p99 increased 24.3%
with tight dispersion. Earlier forms regressed throughput by 23.6–27.9%.

The parser replacement was removed. ASGI retains its specialized byte parser and
the new request-count policy. Duplicating policy is undesirable, but an 18–19%
hot-path regression is not an acceptable way to avoid it.

Rejected-run artifacts are:

- `connection-policy-paired-2026-07-10.json` (incremental parser, -27.9% ASGI);
- `connection-policy-optimized-paired-2026-07-10.json` (whole-block parser,
  -23.6% ASGI);
- `connection-policy-byte-asgi-paired-2026-07-10.json` (byte-native, -18.1%);
- `connection-policy-byte-asgi-c1-2026-07-10.json` (byte-native concurrency 1,
  -19.4%).

### Accepted narrower ASGI Host validation

A follow-up tested validation inside the specialized ASGI parser. A single
compiled expression over the whole field block closed Host and field-syntax gaps
but still regressed throughput 14.2% and increased p99 22.7% at 64 connections;
it was rejected and removed.

The accepted slice instead counts Host fields while performing the parser's
existing byte split, validates authority bytes with C-level byte operations, and
enforces the same 100-field budget. Missing, duplicate, empty, non-ASCII, userinfo,
whitespace/control, malformed port, and malformed bracketed Host values now get
`400` plus close before ASGI app dispatch. HTTP/1.0 remains valid without Host.

| Scenario | Throughput change | p99 change | Ratio MAD (RPS / p99) |
| --- | ---: | ---: | ---: |
| ASGI 1 KiB, 64 connections, 7 trials | -0.7% | +4.0% | 17.9% / 11.0% |
| ASGI 1 KiB, 1 connection, 11 longer trials | -2.2% | +1.6% | 7.8% / 2.9% |

The 64-connection run was noisy, so the longer concurrency-one result is the
resolution gate. Both stay inside the protected 5% budget and the client was not
saturated. `asgi-strict-host-paired-2026-07-10.json` and
`asgi-strict-host-c1-long-2026-07-10.json` are the accepted artifacts;
`asgi-strict-byte-paired-2026-07-10.json` records the rejected full-block form.

Strict non-Host field-name/value syntax remains a documented ASGI gap. Future
work should find a cheaper validation primitive or accept a separately justified
standards cost; it must not quietly restore the rejected 14% regression.

## Follow-up

The next connection-state slice should model unread body ownership, pipelined
head ownership, deadline phases, and error serialization. It must preserve the
zero-cost-disabled shape of this setting and distinguish HTTP/1 requests from
multiplexed HTTP/2/3 streams. Request-count recycling can later compose with a
supervisor, but it does not by itself provide worker replacement or graceful
reload.

# HTTP/1 keep-alive idle timeout — 2026-07-10

Status: accepted as an opt-in connection-occupancy policy; the default inherits
the existing active timeout and therefore preserves current behavior.

## Decision

Add `keepalive_timeout` to `Config` and `--keepalive-timeout SECONDS` to the CLI.
It controls how long an HTTP/1 connection may sit between responses and the next
request. When unset, the existing `--timeout` value remains the only timeout.
Values must be positive.

The policies are deliberately separate:

- `timeout` bounds active socket operations and ASGI request-head/body waits;
- `keepalive_timeout` may be shorter to release an idle thread, task, connection
  admission slot, and file descriptor sooner;
- `max_requests_per_connection` bounds reuse by count rather than elapsed time.

A short idle timeout improves occupancy under many dormant clients, especially
with the optional bounded thread pool, but creates more TCP and TLS handshakes for
clients that pause between requests. No profile selects a new value yet: the
right default depends on real idle-connection and handshake measurements. This
is why the policy is configurable rather than silently shortening the existing
30-second behavior.

The setting applies to threaded/static, CGI, proxy, WSGI, and ASGI HTTP/1. It
does not apply to multiplexed HTTP/2 or HTTP/3 connections, which need separate
stream/session policies.

## Implementation boundary

The threaded adapter keeps the original stdlib loop when the option is unset.
When enabled, it waits for the first byte of a subsequent request under the idle
budget, restores the active timeout, and then calls the normal request parser and
dispatcher. Pipelined bytes are already buffered and therefore do not incur an
idle wait. WSGI calls the same HTTP/1 loop rather than bypassing the policy.

ASGI selects one of two loops once per connection. The default loop is kept hot
and unchanged. The configured loop uses the idle value for the next complete
request head, then uses the active timeout for body reads and response work. This
is slightly stricter than the threaded first-byte boundary, but both guarantee
that a completely idle persistent connection is released by the configured
deadline. A future shared connection state machine should make header-phase
deadlines identical without adding overhead to the disabled path.

Tests prove that static, WSGI, and ASGI connections close after one successful
response and an idle interval, while the independent active timeout remains
larger. Configuration and CLI tests cover inheritance, propagation, and rejection
of zero/negative values.

## Performance gate

The paired comparison used CPython 3.15.0b3 with the GIL enabled, one server CPU,
two client processes, 64 connections, seven rotated trials, 0.75 second warmup,
and 3 second samples. Candidate and baseline both leave the new policy unset.

The first ASGI shape checked/passed idle state on every request and was rejected:

| Scenario | Throughput change | p99 change | Decision |
| --- | ---: | ---: | --- |
| static 1 KiB | -1.1% | -0.5% | neutral |
| WSGI 1 KiB | +0.6% | +4.5% | neutral, noisy tail |
| ASGI 1 KiB | -6.5% | +2.7% | reject implementation shape |

After selecting the default/idle ASGI loop once per connection, the candidate
measured -2.4% throughput / +1.6% p99 at 64 connections. Because throughput ratio
MAD was 9.1%, an 11-trial, five-second concurrency-one resolution run was added;
it measured +5.8% throughput / -2.0% p99 with 8.5% / 4.9% ratio MAD. The result is
classified as neutral, not an improvement. Static and WSGI source did not change
after their neutral gate.

All samples completed with zero errors and without client saturation. The final
implementation stays inside the protected 5% regression budget. Ignored local
artifacts are:

- `keepalive-timeout-paired-2026-07-10.json` — initial three-cohort run and
  rejected ASGI shape;
- `keepalive-timeout-asgi-optimized-2026-07-10.json` — final ASGI c64 gate;
- `keepalive-timeout-asgi-c1-long-2026-07-10.json` — longer resolution gate.

## Follow-up

This closes only idle connection occupancy. A total body-read budget and
write-progress deadline are now separate accepted settings; a total production
request-head deadline was subsequently closed by
[Total HTTP/1 request-head timeout](2026-07-11-request-head-timeout.md). Phase policies use explicit names rather
than overloading this setting. A
production selector must also expire idle state without an O(number of
connections) scan on every loop iteration.

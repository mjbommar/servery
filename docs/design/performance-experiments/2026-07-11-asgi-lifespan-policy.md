# ASGI lifespan policy, failure, and state — 2026-07-11

Status: accepted in production ASGI serving. The default remains compatibility-
oriented `auto`; strict and disabled modes are explicit operator choices.

## Problem and specification boundary

Servery previously started one best-effort lifespan task, waited up to five
seconds, and bound the listener even after an explicit
`lifespan.startup.failed`. It also ignored `lifespan.shutdown.failed` and did not
provide the optional lifespan `state` namespace to HTTP or WebSocket scopes.

That conflated three different cases:

- an application that does not implement lifespan;
- an application that explicitly reports failed initialization; and
- an application that claims lifespan but never completes a phase.

The [ASGI lifespan 2.0 specification](https://asgi.readthedocs.io/en/latest/specs/lifespan.html)
requires a server to wait for startup completion before accepting connections,
exit after explicit startup failure, wait for shutdown completion, and terminate
after explicit shutdown failure. It deliberately permits an initial application
exception to mean “lifespan unsupported.” It also defines a server-managed
state namespace that is shallow-copied into request scopes.

Uvicorn exposes `auto`, `on`, and `off` lifespan policy. Servery now uses the
same operator vocabulary while retaining its existing bounded wait as an
explicit setting.

## Accepted policy

New configuration:

| CLI | `Config.create()` | Default | Behavior |
|---|---|---|---|
| `--lifespan auto\|on\|off` | `lifespan` | `auto` | detect support, require support, or skip the protocol |
| `--lifespan-timeout SECONDS` | `lifespan_timeout` | `5.0` | positive per-phase startup/shutdown wait |

The modes are deliberately distinct:

- `auto` treats an exception or invalid non-lifespan response before startup
  completion as unsupported and proceeds without lifespan state. This preserves
  compatibility with simple ASGI callables.
- `on` treats the same behavior as startup failure. Use it when application
  resources must be initialized before readiness.
- `off` never creates the lifespan task and never adds request-state copy work.
  It is appropriate only for an application known not to need lifespan.

In both `auto` and `on`, explicit `lifespan.startup.failed` prevents socket bind.
An explicit failure message reaches the CLI as a clean nonzero error without a
traceback. Startup or shutdown timeout is also fatal. Explicit
`lifespan.shutdown.failed`, premature exit after supported startup, malformed
message ordering, and shutdown timeout are surfaced while the server terminates.

The timeout stays configurable because application initialization and cleanup
budgets are deployment-specific. Removing it would allow a broken application
to hang process startup or shutdown indefinitely; silently continuing after it
would advertise false readiness.

## State ownership and cost

The lifespan scope receives one server-owned `state` dictionary. After
successful startup, each HTTP and WebSocket scope gets a shallow copy:

- top-level request mutations do not leak into later scopes;
- nested values remain shared, as required by the ASGI specification; and
- `off` or auto-detected unsupported applications omit `state` entirely and do
  not allocate a per-request copy.

This makes the correctness/performance choice explicit. Frameworks that support
lifespan get spec-shaped state; minimal callables retain their prior hot path.

If listener bind fails after successful startup, servery sends shutdown before
propagating the bind error. This avoids leaking resources initialized during
startup.

## Tests

Direct and wire tests cover:

- auto-detected unsupported applications;
- strict `on` rejection of the same callable;
- `off` bypass of an application that would explicitly fail startup;
- startup failure preventing the ready/bind callback;
- bounded startup hangs;
- explicit shutdown failure;
- clean CLI failure text and exit status;
- top-level isolation plus nested sharing across HTTP request state copies; and
- lifespan state in WebSocket scopes.

Existing ASGI callables that try to send an HTTP response for every scope remain
compatible in `auto`; they fail in `on`, as intended.

## Performance evidence

The candidate image is
`sha256:17d323d8f0768de58315b80419654472928bad52141a8fd99d237ba1164a6eb1`
with product-tree hash `e3e64c86...`. The frozen pre-lifespan-state image is
`sha256:d2d79e7b...` with product-tree hash `28c8123e...`.

Artifacts:

- `benchmarks/artifacts/lifespan-state-auto-final-2026-07-11.json`;
- `benchmarks/artifacts/lifespan-off-control-final-2026-07-11.json`; and
- `lifespan-state-source-smoke-2026-07-11.json`.

Five five-second trials use one server CPU, four client processes, 64
connections, exact response validation, deterministic order rotation, and zero
timed errors.

| Workload/policy | RPS change | RPS ratio MAD | p99 change | p99 ratio MAD | Decision |
|---|---:|---:|---:|---:|---|
| Minimal ASGI, auto unsupported | +3.58% | 1.00 points | -2.63% | 2.33 points | neutral; no state copy |
| FastAPI JSON, auto supported | -1.78% | 1.55 points | +0.94% | 0.82 points | neutral |
| Starlette JSON, auto supported | -0.74% | 7.10 points | -1.34% | 8.61 points | noisy neutral |
| Starlette JSON, off | +1.22% | 5.65 points | -0.45% | 5.88 points | noisy neutral control |

Cgroup peak memory is unchanged within measurement resolution. The source gate
does not justify making state nonconformant or disabling lifespan by default.

## Remaining lifecycle gap

This closes startup/shutdown message semantics and state propagation, not the
whole graceful process-shutdown problem. `asyncio.Server` stops accepting new
connections before lifespan shutdown, but servery does not yet maintain a
global registry that drains active HTTP/WebSocket application tasks and
post-response background work before sending `lifespan.shutdown`.

The next lifecycle slice must add bounded connection/task drain with an
operator-configurable graceful-shutdown deadline, cancellation behavior, and
tests for in-flight responses and background tasks. It should not claim
production parity from lifespan messages alone.

## Verification

- 848 functional tests pass on CPython 3.15 with the GIL, with four optional
  integration skips.
- The same 848 tests pass on free-threaded CPython 3.15, with the same four
  skips.
- Focused ASGI/CLI and comparison tests cover lifecycle behavior, state,
  explicit adapters, and the frozen framework baseline.
- Ruff, formatting, type checks, `git diff --check`, and a strict documentation
  build pass after the lifecycle changes.
- Wheel and sdist builds succeed; both install outside the repository, and the
  installed CLI/API expose and validate the new lifespan settings with zero
  unconditional runtime dependencies.

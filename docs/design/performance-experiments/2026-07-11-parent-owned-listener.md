# Parent-owned TCP listener seam — 2026-07-11

Status: accepted as the implementation seam for `EDGE-011`. This is not a
supervisor implementation.

## Decision

TCP bind/listen policy now lives in `servery._listener`. A future parent process
can call `bind_tcp_listener()` once, retain that socket, and give the threaded or
ASGI runtime an already-bound listener. Both runtimes validate that the socket is
an open, listening TCP stream with the address family implied by the configured
host.

Adoption duplicates the descriptor. The caller owns and closes the original;
the runtime owns and closes only its duplicate. Consequently, closing or failing
one runtime cannot invalidate the parent's handle, and the parent may close its
copy after a worker has adopted it without stopping that worker. POSIX duplicate
descriptors still share kernel file-status flags; the ownership guarantee is
about descriptor lifetime, not independent blocking-mode state.

TLS remains worker-local. The threaded runtime wraps only its adopted duplicate,
and the ASGI event loop receives only its duplicate. The parent's retained
listener is never replaced with an `SSLSocket` and owns no worker TLS context.

## Compatibility

`make_server(config)` remains the direct bind-and-activate API. Internally it
uses the same binder and transfers an adopted descriptor to the existing server
object. Port zero still delegates ephemeral selection directly to the kernel.
For a busy nonzero port, only address-in-use triggers the existing bounded
forward scan, and the selected port remains reflected in the server's effective
configuration. Other bind errors still surface immediately.

`make_server(config, listener=socket)` and
`asgi.serve_forever(config, listener=socket)` are the explicit adoption seams.
Supplying a listener disables runtime bind and port scanning: the socket's bound
address is authoritative. Existing callers that omit it retain their prior APIs
and behavior.

## Evidence

Deterministic threaded and ASGI tests cover both sides of ownership:

- runtime shutdown leaves the caller's listening descriptor valid;
- caller shutdown after adoption leaves the runtime able to serve requests;
- two independently constructed threaded generations adopt the same parent
  listener, serve distinct roots in sequence, and close independently;
- TLS wraps the threaded runtime's duplicate while the parent descriptor stays
  a raw listening socket;
- a bound but non-listening socket is rejected without being closed;
- direct ephemeral binding and busy-port scanning remain covered; and
- ASGI listener-validation failure runs lifespan shutdown and preserves the
  caller's invalid socket for caller-owned cleanup.

The seam intentionally does not define worker generation, fork/spawn descriptor
transfer, readiness, restart, or reload policy. Those belong to the supervisor
tasks that consume this boundary.

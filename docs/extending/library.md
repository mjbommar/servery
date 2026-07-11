# Using servery as a library

servery is a CLI, but it's also a small, importable library. The public API is
deliberately tiny:

```python
from servery import (
    Config,            # immutable, validated configuration
    serve,             # build + run a server (blocking)
    make_server,       # build a server you drive yourself
    server_url,        # the URL a bound server is listening on
    ServeryHTTPServer, # the threading server
    ServeryHandler,    # the request handler
    __version__,
)
```

## Serve a directory

```python
from servery import Config, serve

serve(Config.create("./public", host="127.0.0.1", port=8000))
```

`serve()` blocks until interrupted. `Config.create()` validates everything up front
and returns a **frozen** `Config` (immutable — safe to share across threads, which is
what makes servery free-threading-friendly).

## Drive the server yourself

`make_server()` binds and returns the server without running its loop — useful when
you need the bound port, want to run it in a background thread, or embed it:

```python
import threading
from servery import Config, make_server, server_url

server = make_server(Config.create("./public", port=0))  # port 0 = ephemeral
print("listening on", server_url(server))                # e.g. http://127.0.0.1:54321/

thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
# … do work, hit the server …
server.shutdown()
```

This pattern is ideal for **tests** — spin up a real server on an ephemeral port,
exercise it over HTTP, and shut it down.

## Common configuration

`Config.create()` accepts keyword arguments mirroring the CLI flags. A few:

```python
Config.create(
    "./public",
    host="0.0.0.0",          # --bind
    port=8000,               # --port
    auth="me:secret",        # --auth
    upload=True,             # --upload
    allow_overwrite=False,   # --allow-overwrite
    partial_upload_ttl=86400,
    max_partial_uploads=128,
    tls_self_signed=True,    # --tls-self-signed
    tls_cert="cert.pem",     # --tls-cert
    tls_key="key.pem",       # --tls-key
    cors=True,               # --cors
    spa=True,                # --spa
    cache_max_age=3600,      # --cache
    compress=True,           # gzip (on by default)
    max_compress_size=10 * 1024 * 1024,
    compression_cache_size=32 * 1024 * 1024,
    max_buffered_response=1024 * 1024,
    small_file_buffer_size=16 * 1024,
    max_connections=256,
    keepalive_timeout=10.0,
    request_head_timeout=30.0,
    request_body_timeout=300.0,
    write_timeout=30.0,
    max_requests_per_connection=1000,
    http2=True,              # --http2
    max_h2_streams=100,
    max_workers=8,           # --max-workers
)
```

For an ASGI application, lifecycle policy is explicit:

```python
Config.create(
    ".",
    asgi_app="myapp:app",
    lifespan="on",          # auto | on | off
    lifespan_timeout=10.0,  # per startup/shutdown phase
)
```

The resource settings describe observable policy rather than internal chunk sizes.
For example, lower `max_buffered_response` to trade a little small-file throughput
for lower per-stream memory, set it to zero to force streaming, or keep the default
1 MiB hybrid path. `small_file_buffer_size` controls the separate plaintext HTTP/1
crossover: small files use one bounded read/write while larger files retain
zero-copy `sendfile`; zero forces `sendfile` for every nonempty file.
`max_connections`, `max_workers`, `max_h2_streams`, and
`max_tftp_transfers` are deliberately independent because their resources have
different costs. `max_requests_per_connection` is a separate HTTP/1 lifecycle
policy: `0` permits unlimited reuse, while a positive value closes after that
many requests. A finite count trades reconnect/TLS work for bounded connection
reuse; it does not replace idle, read, or write timeouts.
`keepalive_timeout` separately bounds idle HTTP/1 reuse between requests. Leave
it as `None` to inherit `timeout`, or choose a shorter positive value to release
idle connection/thread/task capacity sooner at the cost of more reconnects and
TLS handshakes.
`request_head_timeout` is a distinct optional total HTTP/1 request-line and
field budget. It starts after the first byte satisfies the idle phase and does
not reset on progress. Leave it as `None` to avoid configured parser/timer cost
and tolerate unrestricted slow heads, or choose a positive value for exposed
origins based on the slowest legitimate cookie/proxy head and link.
`request_body_timeout` is a distinct optional total HTTP/1 body-consumption
budget. It starts on the first nonempty read, does not reset on progress, and
includes application pauses between reads. Leave it as `None` for unrestricted
large/slow bodies or choose a positive value based on the slowest legitimate
upload and chunk-processing cadence.
`write_timeout` is an independent, optional progress deadline for response
writes across transports. It resets after progress; it is neither a maximum
response duration nor a bandwidth limit. Leave it as `None` to preserve native
transport behavior and avoid async timer overhead.

Invalid combinations (e.g. `--dav-write` without `--dav`) raise `ValueError` at
`create()` time, not mid-request. See the [CLI reference](../reference/cli.md) for
the full flag list — every flag maps to a `Config.create()` keyword.

## Want to mount an app instead of files?

servery can serve a WSGI / ASGI / CGI application, or reverse-proxy to an upstream —
see [Running apps](apps.md).

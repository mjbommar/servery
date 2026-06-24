# HTTP/2, HTTP/3 & concurrency

servery serves HTTP/1.1 by default and can step up to HTTP/2 and HTTP/3. See
[Transports](../TRANSPORTS.md) for the design rationale behind the tiering.

## HTTP/2

```bash
servery --http2 --tls-cert cert.pem --tls-key key.pem
```

`--http2` enables a **pure-stdlib** HTTP/2 server — the HPACK header compression and
the binary framing are implemented against the RFCs with no third-party package. It
negotiates `h2` via ALPN over TLS, and also supports `h2c` (cleartext, prior
knowledge) for testing:

```bash
servery --http2          # h2c on plain HTTP, for local testing
```

The HTTP/2 path serves files, listings, gzip, and — like HTTP/1.1 — sends `ETag` +
`Last-Modified` and honors conditional requests (`304`). (Range requests stay on the
full-featured HTTP/1.1 path.)

## HTTP/3 (optional)

HTTP/3 runs over QUIC, which needs AEAD packet protection and a TLS-1.3-in-QUIC
handshake that the standard library doesn't provide — so it's the one **opt-in**
exception to zero-dependency, behind the `servery[http3]` extra:

```bash
# run ad-hoc with the extra, via uv:
uvx --from 'servery[http3]' servery --http3 --tls-cert cert.pem --tls-key key.pem

# …or install it (uv or pip):
uv tool install 'servery[http3]'   # or: pip install 'servery[http3]'
servery --http3 --tls-cert cert.pem --tls-key key.pem
```

The core stays dependency-free; only HTTP/3 pulls in the reference QUIC stack
(`aioquic`).

## Tuning concurrency

servery runs one thread per connection by default. Under high concurrency that can
thrash; bound it to a worker pool:

```bash
servery --max-workers 8
```

Set `N` near your CPU core count to sharply lower tail latency under load. servery
also runs cleanly on the **free-threaded** (no-GIL) CPython builds (3.13t/3.14t) —
the configuration is immutable and there's no module-level mutable state.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--http2` | off | HTTP/2 (ALPN `h2` over TLS, or `h2c` cleartext) |
| `--http3` | off | HTTP/3 over QUIC (needs TLS + `servery[http3]`) |
| `--max-workers N` | unbounded | bound concurrency to N worker threads |
| `--timeout SECONDS` | `30` | per-connection socket timeout (Slowloris bound) |

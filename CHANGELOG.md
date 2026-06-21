# Changelog

All notable changes to servery are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[semantic versioning](https://semver.org/).

## [1.0.0] — 2026-06-21

First stable release. A zero-dependency, pure-Python HTTP file server.

### Added

- **HTTP/1.1 file serving** (pure stdlib): rich, sortable (`?C=&O=`), searchable
  (`?q=`) directory listings with sizes and modified times; index documents.
- **RFC 9110 downloads**: `Range`/`206`/`416`, strong `ETag`s, the conditional
  ladder (`If-None-Match`/`If-Modified-Since`/`If-Range` → `304`/`412`), and
  zero-copy `socket.sendfile()` with a userspace fallback.
- **TLS/HTTPS** via `--tls-cert`/`--tls-key`, ALPN, HSTS over TLS, `--tls-help`.
- **HTTP Basic Auth** (`--auth`): single credential or pre-hashed
  `user:sha256:…`/`sha512`, constant-time comparison, no-TLS warning.
- **Upload** (`--upload`): streaming `multipart/form-data` parser (no `cgi`),
  atomic `os.replace`, bounded size (`--max-upload-size`), `--allow-overwrite`.
- **Archive download**: stream any directory as `tar.gz`/`zip` (`?archive=`),
  chunked, `Content-Disposition` with `filename*`, symlink-safe.
- **CORS** (`--cors` + preflight), **SPA fallback** (`--spa`), **cache control**
  (`--cache`), and secure headers (`nosniff` everywhere, scoped CSP +
  Referrer-Policy on generated pages; `--no-security-headers`).
- **Hardening**: `logging`-module access logs, default socket timeout
  (`--timeout`), optional bounded concurrency (`--max-workers`).
- **HTTP/2** (`--http2`): a pure-stdlib HTTP/2 server — HPACK (RFC 7541) and the
  frame codec (RFC 9113) implemented from the RFCs, ALPN `h2` + h2c, per-stream
  flow control, and DoS limits (concurrent-stream cap, header-block cap, RST
  budget). Verified against `curl --http2`.
- **HTTP/3** via the optional `servery[http3]` (aioquic) extra; the core stays
  zero-dependency. `servery._oscrypto` provides AES-256-GCM via `ctypes` → OS
  OpenSSL (NIST-vector verified) as the zero-PyPI-dependency crypto foundation.
- **Safe defaults**: localhost bind, path-traversal + symlink-escape containment
  (`realpath` + `commonpath`), exposure/cleartext-auth warnings.
- **Free-threading** support (3.13t/3.14t), full type hints (`ty`-checked), and a
  CI gate that enforces zero runtime dependencies in the core wheel.

[1.0.0]: https://github.com/mjbommar/servery/releases/tag/v1.0.0

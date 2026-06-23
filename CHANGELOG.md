# Changelog

All notable changes to servery are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Tooling

- **Benchmark suite** (`benchmarks/`, pytest-benchmark; new opt-in `bench` dependency
  group). Reproducible per-request latency for every transport — HTTP/1.1, TLS, HTTP/2
  (ALPN + h2c), HTTP/3, WSGI, CGI, ASGI, reverse proxy — plus the internal hot paths
  (HPACK, frame codec, listing render, Range parsing, head builder, cert generation).
  `scripts/run_benchmarks.sh` emits a JSON artifact and gates median regressions vs the
  last saved run; HTTP/3's QUIC round-trip runs on a GIL build via `--extra http3`. See
  [BENCHMARKS.md](BENCHMARKS.md). The functional suite stays unittest-based; pytest is
  scoped to `benchmarks/` only.

### Performance

A measured pass over the async/parallel paths (server out-of-process, async
`scripts/loadgen.py` over loopback; `scripts/abdriver.py` manages the lifecycle):

- **ASGI request parsing**: read the whole request head in one
  `readuntil(b"\r\n\r\n")` instead of one `await` per header line.
  **+6%** (102.5k → ~109k req/s keep-alive, c=64; scales with header count).
- **Listen backlog** raised 5 → 128 (`request_queue_size`). Under connection
  churn (`--close`, c=500): **3.3k → ~6.4k req/s and 296 → 0 connection errors**.
- **Characterization** (no code change): file serving is **I/O/syscall-bound**
  (`recv`/`sendfile` release the GIL), so free-threading (3.14t) gives it **no**
  throughput gain over the GIL build (82.9k ≈ 82.3k req/s) and the path is already
  syscall-lean. The single-loop **ASGI** server is ~1-core-bound (~109k req/s).
  Under high concurrency the unbounded thread-per-connection default thrashes
  (c=128: 53k req/s, p99 6.2 ms); **`--max-workers` ≈ CPU cores** fixes it
  (57.5k req/s, **p99 0.40 ms — ~15× lower tail latency**) — now noted in `--help`.

### Observability

- **Unified logging / telemetry / error handling across every transport**
  (HTTP/1.1, /2, /3, CGI, WSGI, ASGI). The stderr log format now carries a level
  (`%(levelname)s`) so access lines (INFO) and problems (WARNING/ERROR) are
  distinguishable and filterable. A consistent vocabulary: **INFO** = access log,
  **WARNING/ERROR** = a handled-but-notable failure, **DEBUG** = swallowed client
  noise. Concretely:
  - **ASGI** gained an access log and, crucially, no longer drops app exceptions
    silently — an unhandled app error is logged with a traceback and returns a
    500 (was an unhandled-task traceback with no response). Lifespan that doesn't
    complete is DEBUG-logged.
  - **WSGI** app errors now return a 500 + ERROR log (were propagating to the
    server with no response).
  - **CGI** failures surface the cause: timeouts (WARNING), exec failures (ERROR),
    and non-zero exits now log the **script's own stderr** (previously discarded).
  - **server.handle_error** DEBUG-logs swallowed client transport errors and
    routes genuinely unexpected errors through the logger (with traceback)
    instead of socketserver's raw stderr.
  - **HTTP/2** logs connection errors + GOAWAYs at DEBUG; **HTTP/3** has a per-
    request access log and a startup banner.

### Security

- **Hardened the TLS cipher suite** to forward-secret **AEAD only** (TLS 1.2
  restricted to `ECDHE+AESGCM`/`ECDHE+CHACHA20`; TLS 1.3 is all-AEAD already).
  Dropping CBC suites removes the Lucky13/SWEET32 surface. Validated with
  `testssl.sh` (`make scan-tls`): TLS 1.2/1.3 only, FS offered, every CVE check
  clean. A failed TLS handshake from an old/scanning client no longer prints a
  server-side traceback (`handle_error` swallows client-side transport errors).

### Added

- **`--profile NAME`**: launch presets that bundle common flags (a defaults layer
  — explicit flags still win). `share`/`inbox`/`public-readonly`/`public-readwrite`
  /`cdn`/`dev`/`app`/`local`. Network-exposed + writable profiles (`inbox`,
  `public-readwrite`) *require* `--auth`, so an open writable public server can't
  be a one-flag accident; TLS profiles default to self-signed (a `--tls-cert`
  upgrades to a real cert).
- **ASGI over TLS** and **ASGI WebSockets**: `--asgi` is no longer HTTP-only.
  HTTPS works via the shared cert machinery (`servery/_tls.py`, now used by both
  servers). WebSockets are implemented from RFC 6455 in pure stdlib
  (`servery/_websocket.py` — handshake, masked frames, fragmentation, ping/pong/
  close); a real **Starlette WebSocket endpoint** runs unmodified (and over `wss`).
- **`--upload-extract`** (requires `--upload`): securely expand an uploaded
  zip/tar into the target dir. Hardened against the classic archive CVEs —
  zip-slip/traversal (realpath containment), symlink/hardlink/device entries
  (skipped, never created), and zip bombs (uncompressed-size + entry-count caps
  enforced on bytes written). `servery/_extract.py`.
- **`--proxy PREFIX=UPSTREAM`** (repeatable): reverse-proxy matching requests to a
  backend and stream the response back — serve static files and proxy `/api` from
  one process. Strips hop-by-hop headers, injects `X-Forwarded-For/-Proto/-Host`,
  bounds the proxied body, 502 on upstream failure. `servery/_proxy.py`.
- **`--wsgi module:app`** (opt-in, off by default): host a WSGI (PEP 3333)
  application instead of files — phase D1 of `docs/DYNAMIC.md`. A lean,
  zero-dependency HTTP/1.1 engine (keep-alive; one write + `Content-Length` for
  materialized bodies; chunked for streaming) rather than the HTTP/1.0 `wsgiref`
  server; PEP 3333 compliance is gated by `wsgiref.validate` in the tests.
  ~20k req/s single-core. HTTP/1.1 only (rejected alongside `--http2`).
- **`--cgi DIR`** (opt-in, off by default — *executes code*): run CGI/1.1
  (RFC 3875) scripts from a cgi-bin directory — phase D2 of `docs/DYNAMIC.md`.
  Pure-stdlib `subprocess` (`shell=False`, clean minimal env, hard timeout,
  bounded body, realpath containment). Security mitigations are built in and
  tested: **httpoxy** (`Proxy`→`HTTP_PROXY` never set), no `Authorization`
  forwarding (RFC 3875 §9.2), `..` traversal cannot escape the cgi dir. Inherent
  process-per-request cost (~spawn-bound).
- **`--asgi module:app`** (opt-in, experimental, HTTP only): host an ASGI 3.0
  application — phase D3 of `docs/DYNAMIC.md`. A small self-contained asyncio
  HTTP/1.1 server ("mini-uvicorn" in pure stdlib): the HTTP scope with keep-alive
  + Content-Length/chunked framing, plus the lifespan protocol (degrades
  gracefully if the app doesn't support it). ~19k req/s single-core; verified to
  run a real **Starlette** app (request + full startup/shutdown lifespan), and a
  full **FastAPI** app over HTTP — pydantic validation (422), streaming responses,
  redirects, exception→500, chunked request bodies, `/docs` and `/openapi.json`
  (12/12 feature checks). Now also supports **TLS/HTTPS** (shared cert machinery)
  and **WebSockets** (see below). HTTP/1.1, single event loop.
- **`--tls-self-signed`**: zero-dependency HTTPS with an ad-hoc certificate
  generated at startup (pure-stdlib RSA-2048 via `servery._certgen` — no
  `cryptography`, no `openssl` binary, no `ctypes`; works on a bare Windows/Linux
  Python). For opportunistic encryption on a dev box or LAN — clients see an
  untrusted-certificate warning (it is not a trust anchor). Mutually exclusive
  with `--tls-cert`. Publicly-trusted/ACME certs remain a (future) optional
  `servery[acme]` extra; see `docs/TRANSPORTS.md` for the TLS tier boundary.

## [1.1.1] — 2026-06-22

First release published to PyPI.

### Fixed

- **Directory listing on touch devices**: the per-file download button was
  hover-only (invisible/untappable on phones); it is now shown via
  `@media (hover: none)`, which also enlarges the facet chips, theme toggle, and
  pager to finger-sized tap targets. Long filenames get `overflow-wrap` so they
  can't force horizontal scroll on a narrow screen.

### Changed

- **Publishing**: releases go to PyPI via GitHub Actions **Trusted Publishing**
  (OIDC) — no API token is stored anywhere.
- **Packaging**: the version is single-sourced from `servery/_version.py`; added
  the `Changelog` project URL and the `Programming Language :: Python :: Free
  Threading` classifier.
- **CI/dev**: bumped `actions/checkout` (v7), `astral-sh/setup-uv`,
  `gitleaks-action`, and the `bandit` floor (`>=1.9.4`).

## [1.1.0] — 2026-06-22

### Added

- **Directory-listing UI/UX pass** (still zero-dependency, server-side, **no
  JavaScript**, and safe under the existing strict CSP):
  - Clickable **breadcrumb** trail in the heading.
  - Per-type **file icons** (extension-based, with a stdlib `mimetypes` fallback
    for long-tail extensions — a pure lookup, no file content is read) and
    **relative timestamps** ("3h ago", exact time on hover).
  - Inline **size bars** and an aggregate **metrics strip** (file/dir counts,
    total size, largest, newest).
  - **`?ext=` file-type facet** chips alongside the existing `?q=` filter.
  - Pure-**SVG modification timeline** histogram.
  - **Per-file download** affordance (`?download=1` forces
    `Content-Disposition: attachment`).
  - **Pagination** for large directories (`?page=`, 1000 rows/page).
  - Cookie-backed **light/dark/auto theme** toggle (`?theme=`).
  - Friendly **empty / no-match** states, sticky table header, `aria-sort`, and
    visible focus styles.

### Performance

A second profiling-driven pass (cProfile / strace / timeit, benchmarked
before/after each change):

- **HTTP/2 HPACK**: Huffman coding is now opt-in on the encoder (raw literals by
  default — for a file server the CPU it costs outweighs the few header bytes it
  saves). Also fixed an O(n²) bit accumulator in `huffman_encode`. **+20%** h2
  throughput.
- **HTTP/2 framing**: pack the 9-octet frame header in a single `struct` call
  (was two packs + concatenations, ~4 allocations), byte-for-byte identical.
  **+9%** h2; combined h2 throughput **+31%** (~11.6k → ~15k req/s, 1-core).
- **Path containment**: `security.is_contained` uses a separator-anchored prefix
  test on POSIX (≈15× faster than `os.path.commonpath`, exact-match verified;
  Windows keeps `commonpath`). Runs on every request. **+3%** small-file.
- **Listing render**: quote each entry name once (not twice) and cache file-type
  extension lookups. **+5–6%** render.

## [1.0.2] — 2026-06-22

A profiling-driven performance pass (cProfile + strace, benchmarked before/after
each change with a new single-core `scripts/microbench.py`).

### Performance

- **Fast request-header parser**: the stdlib's email-based
  `http.client.parse_headers` dominated per-request CPU (MIME/multipart work HTTP
  never needs). Replaced with a line-based reader + a minimal case-insensitive
  header map. Faithful `parse_request` (limits, versions, 0.9, expect-100, obs-fold
  per RFC 9112 §5.2 preserved). Small-file serving **8,896 → 10,766 req/s (+21%)**
  single-core; **~42k → ~52k req/s (+23%)** at 16-way concurrency.
- **Listing render**: `time.localtime` + manual date formatting instead of
  `strftime`, and dropped a redundant `html.escape` on the already-percent-encoded
  href. 50-entry listing **2,486 → 2,843 req/s (+14%)** single-core.
- **Fewer syscalls per file request**: send the body in one `sendfile` (was two);
  skip the SPA `os.path.exists` stat when SPA is off (the default); drop a
  `tell()` `lseek`. Per small-file GET: `sendfile` 2→1, `stat` 2.2→1.2,
  `lseek` 3→2 (≈13→11 syscalls).
- Cached the constant `Server` header; guard access logging on the log level so a
  disabled (quiet) logger does no per-request formatting.

Cumulative: small-file throughput **+24%** single-core. The large-file `sendfile`
path was already ~2.5 GB/s. No API or behavior changes; 295+ tests still pass.

### Added (tests)

- `test_request_parsing.py`: the fast parser (case-insensitivity, first-wins,
  obs-fold, no-colon, EOF-termination, bad version, HTTP/2.0-in-line, HTTP/0.9).
- Listing: an XSS guard proving a hostile filename cannot break out of the href
  now that it is no longer html-escaped, plus an mtime-format check.
- `scripts/microbench.py` (single-core attribution) and a warmup in
  `scripts/bench.py`.

## [1.0.1] — 2026-06-22

Fixes surfaced by a large test-suite expansion (RFC reads + cross-checking
against httpx, curl, and h2spec).

### Performance

- **TCP_NODELAY**: every small response previously incurred a ~40 ms
  Nagle/delayed-ACK stall. Disabling Nagle takes small-file throughput from
  ~390 to ~41,600 req/s (p50 41 ms → 0.28 ms) and listings from ~380 to
  ~14,100 req/s on loopback. The sendfile large-file path was already ~2.5 GB/s.

### Fixed

- Upload: RFC 5987/8187 `filename*` (non-ASCII filenames) was silently dropped;
  now decoded (charset-validated) and preferred over plain `filename`.
- Upload: a plain non-ASCII `filename="naïve.txt"` was mojibake'd — part headers
  are decoded as UTF-8 (RFC 7578 §5.1.1).
- Upload: a zero-part body (just the close-delimiter) is accepted as empty.

### Added (tests)

- HTTP/1.1 conformance (methods, 9 path-traversal vectors, conditional
  precedence, MIME, HEAD==GET, multi-range, empty file) + httpx interop.
- Upload robustness (filename*, multiple files, boundary-in-content, chunked
  rejection) + httpx multipart interop.
- HTTP/2 conformance (h2spec generic 50/52 + hpack 8/8 validated; padded
  HEADERS, HPACK continuity, concurrent streams, malformed-request RST) + httpx
  h2-over-TLS interop.
- Security regression + abuse cases (URI/header limits, Slowloris timeout, CRLF
  injection e2e, upload containment, auth bypass).
- Performance/load smoke (Nagle regression guard, concurrent-correctness under
  free-threading, large-file integrity) + a runnable `scripts/bench.py`.

281 tests total.

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

[1.1.1]: https://github.com/mjbommar/servery/releases/tag/v1.1.1
[1.1.0]: https://github.com/mjbommar/servery/releases/tag/v1.1.0
[1.0.2]: https://github.com/mjbommar/servery/releases/tag/v1.0.2
[1.0.1]: https://github.com/mjbommar/servery/releases/tag/v1.0.1
[1.0.0]: https://github.com/mjbommar/servery/releases/tag/v1.0.0

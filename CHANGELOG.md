# Changelog

All notable changes to servery are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
[semantic versioning](https://semver.org/).

## [1.1.0] — 2026-06-22

### Added

- **Directory-listing UI/UX pass** (still zero-dependency, server-side, **no
  JavaScript**, and safe under the existing strict CSP):
  - Clickable **breadcrumb** trail in the heading.
  - Per-type **file icons** and **relative timestamps** ("3h ago", exact time on
    hover).
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

[1.1.0]: https://github.com/mjbommar/servery/releases/tag/v1.1.0
[1.0.2]: https://github.com/mjbommar/servery/releases/tag/v1.0.2
[1.0.1]: https://github.com/mjbommar/servery/releases/tag/v1.0.1
[1.0.0]: https://github.com/mjbommar/servery/releases/tag/v1.0.0

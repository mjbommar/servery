# Design: on-the-fly gzip content-coding

Status: implemented. Scope: HTTP/1.1 handler (then HTTP/2). Zero-dep (stdlib `gzip`/`zlib`).

## Goal
Transparently compress text-like responses when the client accepts gzip, for a
bandwidth win, without breaking ranges, caching, or the sendfile fast path for
non-compressible content.

## Requirements (from RFC 9110/9111/9112/9113; see research notes)
- **Negotiation (9110 §12.5.3, §12.4.2):** gzip only when `Accept-Encoding` lists
  `gzip` (or `*`) with q > 0. `gzip;q=0`, an empty `Accept-Encoding`, or gzip absent
  with no matching non-zero `*` ⇒ serve identity. Never 415 a static GET.
- **Codings (§8.4.1):** offer **`gzip` only**. `deflate` is ambiguous (raw vs zlib)
  and unreliable; brotli needs a dependency. Emit `Content-Encoding: gzip`, never
  list `identity`.
- **Vary (§12.5.5, 9111 §4.1):** `Vary: Accept-Encoding` on **every** response for a
  resource we would *conditionally* compress — including the identity response —
  so shared caches key on the header and can't hand gzip to a non-gzip client.
- **ETag (§8.8.3.3):** the gzip representation MUST NOT share a *strong* ETag with
  identity. We suffix the gzip ETag with `-gz` (kept strong, distinct), and decide
  the coding *before* evaluating conditionals so the 304 / `If-None-Match` echo uses
  the coding-correct tag.
- **Range (§14.1.2, §14.2, §14.3):** ranges are computed over the *encoded* bytes,
  which is incoherent for on-the-fly gzip. So **compression and ranges are mutually
  exclusive per request**: if a `Range` is present we serve identity (with
  `Accept-Ranges: bytes` + 206); if we gzip we ignore Range, send a full 200, and do
  **not** advertise `Accept-Ranges`. (Matches nginx, which disables ranges for
  dynamically gzipped responses.)
- **Conditionals (§13.2, §14.2):** evaluate preconditions first; a 304 carries the
  coding-correct ETag and no body; Range is only considered when the result is 200.
- **Framing:** buffer-compress and set an exact `Content-Length` (keeps keep-alive,
  simplest). gzip means **no sendfile** (bytes pass through zlib).
- **Security (BREACH):** a side-channel only when a *secret* and *attacker-controlled
  input* share one compressed body. A static file server reflects no secrets, so it
  does not apply; the directory listing echoes filenames but carries no secret.

## Design decisions
- New `servery/_compress.py`:
  - `accepts_gzip(accept_encoding)` → bool (parses q-values, handles `*`, `gzip;q=0`,
    empty header).
  - `compressible(content_type)` → bool via an **allowlist**: `text/*`, `+json`,
    `+xml`, and an explicit set (`application/json`, `application/javascript`,
    `image/svg+xml`, wasm, fonts ttf/otf, …). Already-compressed media (jpeg/png/
    mp4/zip/woff2/…) are never matched.
  - `GZIP_MIN = 1024` (gzip framing ~18 B; tiny bodies don't benefit), `GZIP_MAX =
    10 MiB` (above the cap we serve identity + sendfile — huge files are usually
    media anyway, and this bounds per-request memory). `gzip_bytes(data)` →
    `gzip.compress(data, compresslevel=6, mtime=0)` (mtime=0 = deterministic output).
- `handler.py`:
  - A `_vary_accept_encoding` instance flag (like `_generated_page`), reset in
    `send_head`, set for compressible files and all listings; `end_headers` emits
    `Vary: Accept-Encoding` when set — DRY and central.
  - `_serve_file`: read `Accept-Encoding` + `Range` up front; `use_gzip = compressible
    and GZIP_MIN ≤ size ≤ GZIP_MAX and no Range and accepts_gzip`. Pick the ETag
    (`-gz` suffix when gzipping) before the 304 check. If `use_gzip`: read → compress
    → 200 with `Content-Encoding: gzip`, exact compressed `Content-Length`, no
    `Accept-Ranges`; return a `BytesIO` (sent through the existing in-memory body
    path). Else the existing identity/range path, now flagged for `Vary`.
  - `list_directory`: gzip the rendered HTML when accepted; always `Vary`.
- HTTP/2 `_build_response` gets the same treatment in a second pass (it already
  buffers the body, so it's a small addition; streams via DATA frames).

## Out of scope (now)
- Streaming gzip for files > `GZIP_MAX` (kept identity + sendfile).
- Precomputed `.gz` sidecar serving (`gzip_static` model) with ranged gzip.
- brotli / deflate.

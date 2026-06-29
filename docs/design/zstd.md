# Design: zstd content-coding (3.14+), gzip fallback

Status: implemented. Scope: the shared coding decision (HTTP/1.1 + HTTP/2 + HTTP/3).
Zero-dep (stdlib `compression.zstd`, PEP 784; gzip otherwise). Companion to
[gzip](gzip.md).

## Goal
Offer `Content-Encoding: zstd` — a better ratio and far faster decode than gzip —
when the running interpreter can produce it and the client accepts it, while keeping
gzip as the universal fallback and changing nothing on Python 3.13.

## Requirements (RFC 9110; RFC 8878 for the `zstd` coding)
- **Honest advertisement (§12.5.5):** never offer a coding we can't produce. zstd is
  3.14-only (`compression.zstd`), so it is advertised *only* when
  `_compress.HAVE_ZSTD` is true. A 3.13 build behaves exactly as before (gzip only).
- **Negotiation (§12.5.3):** q-value aware, `*` and `coding;q=0` handled, absent
  header ⇒ identity. Prefer zstd over gzip when both are acceptable.
- **Per-coding ETag (§8.8.3.3):** each representation gets a distinct strong ETag —
  `-gz` for gzip, `-zst` for zstd — decided before conditionals so a 304 echoes the
  coding-correct tag.
- **Vary / Range / framing:** unchanged from gzip — `Vary: Accept-Encoding` on every
  compressible response, compression is mutually exclusive with `Range`, buffer-encode
  with an exact `Content-Length` (no sendfile for coded bodies).
- **No brotli, no deflate:** brotli needs a third-party dependency (out, forever);
  deflate is ambiguous.

## Design decisions
- `_compress.py` generalizes the gzip-only helpers without removing them:
  - `HAVE_ZSTD` / `ZSTD_LEVEL` (9 — clear of the slow high end); `_zstd` imported in a
    `try/except ImportError`.
  - `_accepts(accept_encoding, names)` factors the q-value parser shared by
    `accepts_gzip` and `accepts_zstd`.
  - `negotiate(accept_encoding, *, enabled)` — for always-compressible generated HTML
    (the listing): zstd ▸ gzip ▸ None.
  - `choose_encoding(content_type, size, accept_encoding, *, enabled)` — for files:
    the type/size gate, then `negotiate`. Returns `"zstd" | "gzip" | None`.
  - `encode(data, coding)` applies the chosen coding. `should_gzip`/`gzip_bytes`
    stay as back-compat shims.
- `_conditional.coding_variant(etag, coding)` replaces the gzip-only `gzip_variant`
  at the call sites (which is kept).
- `handler._serve_file` / `list_directory` and the shared `_response.build_static` all
  switch from a `gzip: bool` to a `coding: str | None`, so HTTP/1.1, HTTP/2, and
  HTTP/3 share one decision and can't drift.

## Out of scope (now)
- Precompressed `.zst`/`.gz` sidecar serving (`gzip_static`-style).
- A `zstd` content-coding for files above `GZIP_MAX` (kept identity + sendfile).
- The `backports.zstd` PyPI shim on 3.13 (a dependency — out).

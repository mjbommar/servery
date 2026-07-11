# Design: integrity digests (RFC 9530)

Status: implemented. Scope: HTTP/1.1 file responses. Zero-dep (stdlib
`hashlib`/`base64`).

## Goal
Let a client verify a download — especially one reassembled from several parallel
`Range` requests — with a standardized, self-describing header instead of an
out-of-band `.sha256` sidecar, without taxing the default download path.

## Requirements (RFC 9530, Digest Fields)
- **`Repr-Digest` over the full representation.** The digest covers the identity file
  bytes, independent of the range served — so a `206`/parallel download can be
  validated against the whole-file digest. (`Content-Digest`, the per-transfer
  digest, is intentionally not implemented — `Repr-Digest` is the useful one here.)
- **Negotiated, opt-in.** Emitted only when the request carries `Want-Repr-Digest`.
  Computing it requires reading the whole file, so the default GET path neither
  hashes nor adds a header.
- **Algorithms.** `sha-256` and `sha-512` (lowercase keys); the deprecated `md5` /
  `sha` (SHA-1) are never produced. The client's preference (`sha-512=10, sha-256=3`)
  picks; ties break toward sha-256.
- **Wire form (RFC 8941).** A dictionary member with a byte-sequence value:
  `Repr-Digest: sha-256=:<base64>:`.
- **Coding boundary.** Emitted on **identity** responses only (200 and 206), where
  the representation *is* the file on disk. A content-coded (gzip/zstd) response
  describes a different representation, so the digest is omitted there.

## Design decisions
- `servery/_digest.py` provides:
  - `choose_algorithm(want)` — tolerant `Want-*-Digest` parser (bare key, integer
    preference, `?0`/`?1`); returns the RFC key or `None`.
  - `field_value(algorithm, data)`, `field_value_for_file(path, algorithm)`, and
    `field_value_for_handle(handle, algorithm, expected_size)`; file forms stream
    in 256 KiB chunks with flat memory;
  - an entry-bounded `DigestCache` whose zero-entry mode retains nothing but
    shares one transient result among concurrent requests for the same identity.
- HTTP/1 hashes the same opened handle used for metadata and body emission. An
  atomic pathname replacement can no longer pair a new digest with old bytes.
  Exact-size hashing fails before file headers on truncation and restores the
  handle position for range/sendfile delivery.
- Production retains zero digest entries. A public retained-cache setting is
  deferred until metadata invalidation behavior and real workloads justify it.
  The benchmark-only selector explores separate digest worker, queue, and cache
  budgets; hashing never runs on its event loop.

## Out of scope (now)
- `Content-Digest` / per-range or per-coding digests.
- `Repr-Digest` on the HTTP/2 / HTTP/3 buffered backends (HTTP/1.1 is the
  full-featured path, and where parallel-range downloads matter).
- Emitting digests unsolicited, or on directory listings.
- A shipped retained digest cache or selector digest-worker setting.
- HTTP Message Signatures (RFC 9421), for which this is the natural input later.

Performance and scheduling evidence is recorded in
[Opened-identity digests and bounded selector hashing — 2026-07-10](performance-experiments/2026-07-10-selector-digests.md).

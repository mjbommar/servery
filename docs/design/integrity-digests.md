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
- New `servery/_digest.py`, pure functions:
  - `choose_algorithm(want)` — tolerant `Want-*-Digest` parser (bare key, integer
    preference, `?0`/`?1`); returns the RFC key or `None`.
  - `field_value(algorithm, data)` and `field_value_for_file(path, algorithm)` (the
    file form streams in 256 KiB chunks; flat memory).
- `handler.ServeryHandler._send_repr_digest(path)` reads `Want-Repr-Digest`, computes,
  and emits the header. Called in the identity 200 and 206 branches of `_serve_file`
  (after `Last-Modified`, before the body). The double file read (digest + send) is
  acceptable because it happens only on explicit request.

## Out of scope (now)
- `Content-Digest` / per-range or per-coding digests.
- `Repr-Digest` on the HTTP/2 / HTTP/3 buffered backends (HTTP/1.1 is the
  full-featured path, and where parallel-range downloads matter).
- Emitting digests unsolicited, or on directory listings.
- HTTP Message Signatures (RFC 9421), for which this is the natural input later.

# Design: resumable upload (`Content-Range` PUT)

Status: implemented. Scope: HTTP/1.1 handler, behind `--upload`. Zero-dep (stdlib
`os`/`tempfile`).

## Goal
Make uploads survive an interrupted transfer — the upload counterpart to the
download side's `Range`/`206` — using a convention that works from a bare `curl`
with no client library.

## Why the Google/S3 `Content-Range` PUT, not tus
The IETF resumable-upload draft (and tus 1.0) need a client library and, for the
draft, a `104` interim response the stdlib server can't cleanly emit. The
Google/S3-style `Content-Range` PUT is an ad-hoc convention but is scriptable with
two headers and bare `curl`, which fits servery's "no bespoke client" bar. (See the
research brief that informed this choice.)

## Protocol
- `PUT /path` with **no** `Content-Range` → write the whole body (create, or replace
  with `--allow-overwrite`). `201` (created) / `200` (replaced).
- `PUT /path` `Content-Range: bytes <start>-<end>/<total>` → append the chunk at
  `start`. Partial ⇒ `308 Resume Incomplete` + `Range: bytes=0-<last>`; the chunk
  that reaches `total` commits ⇒ `201`/`200`.
- `PUT /path` `Content-Range: bytes */<total>` + empty body → **query**: `308` +
  `Range` reporting bytes stored (no `Range` header when nothing is stored yet).
- Chunks must be **contiguous** (`start` == bytes already stored); a gap ⇒ `409` so
  the client re-queries. `Content-Length` must equal the chunk length.

## Design decisions
- New `servery/_resumable.py` (pure parsing + file ops):
  - `parse_content_range(value) -> ContentRange` (raises `ResumableError` ⇒ 400).
  - `part_path(target)` — a hidden `.<name>.servery-part` sidecar **in the target's
    directory** (a dotfile, so it stays out of listings; same filesystem ⇒ atomic
    rename). `stored_bytes`, `append`, `write_whole`, `commit`, `discard`.
- `handler.do_PUT`: `--dav` ⇒ WebDAV owns PUT; else a matching `--proxy` route; else
  `--upload` ⇒ `_resumable_put`; else `501`.
- `_resumable_put` reuses the one path-safety choke-point (`security.is_contained` +
  `translate_path`) and `upload.BoundedReader` + `--max-upload-size`. Partial data
  accumulates in the sidecar and is committed with an atomic `os.replace` only on the
  final byte, so a half-finished upload never appears at the destination.
- **Keep-alive safety:** errors raised *before* the body is read close the connection
  (`_put_reject`) to avoid desync; the post-read 308/201/409 paths leave it open.
- **Status code:** `308` follows the Google convention (and conflicts with RFC 7538's
  redirect meaning) — acceptable because the protocol already needs a cooperating
  client. Documented as such.

## Out of scope (now)
- The tus / IETF resumable-upload protocol and its `104` interim response.
- Concurrent writers to one target (best-effort; the contiguity check serializes).
- Resumable upload over the HTTP/2 / HTTP/3 backends (HTTP/1.1 is the write path).

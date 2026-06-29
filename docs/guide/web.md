# Compression, caching & headers

servery applies the cross-cutting web behaviors you'd expect from a real static
server — most on by default, each with a flag.

## On-the-fly compression (zstd / gzip)

Text-like responses (HTML/CSS/JS/JSON/SVG/XML — and the directory listing itself)
are compressed when the client accepts a coding. **On by default**; disable with
`--no-compress`.

servery prefers **`zstd`** — better ratio, much faster decode — when the
interpreter provides it (**Python 3.14+**, via the stdlib `compression.zstd` module,
PEP 784) *and* the client sends `Accept-Encoding: zstd`. Otherwise it uses **`gzip`**.
On Python 3.13 (no stdlib zstd) only gzip is offered — zstd is advertised only when
it can actually be produced, so a client never sees a coding the server can't make.

It's RFC 9110-correct:

- q-value-aware negotiation; `br` (brotli) and `deflate` are intentionally not used
  (brotli needs a third-party dependency; deflate is ambiguous).
- `Vary: Accept-Encoding` on every compressible response, and a **distinct ETag** per
  coding (`-gz` / `-zst` suffix) so caches never mix representations.
- Mutually exclusive with `Range`: a range request is served identity, since a byte
  range over coded bytes is incoherent.
- Already-compressed media (jpeg/png/mp4/zip/woff2/…) is never touched — preserving
  the zero-copy `sendfile` fast path.

A typical directory listing compresses ~18×. Compression is applied across HTTP/1.1,
HTTP/2, and HTTP/3.

## Integrity digests (RFC 9530)

A client that wants to verify a download can send `Want-Repr-Digest`; servery answers
with a `Repr-Digest` over the **whole representation** (the file on disk):

```bash
curl -sD- -o out.bin -H 'Want-Repr-Digest: sha-256' http://localhost:8000/big.bin
# ... Repr-Digest: sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:
```

This is the standardized (RFC 9530) replacement for an out-of-band `.sha256` sidecar.
It's emitted only when asked, on identity responses — **including `206` range
responses**, where the digest still covers the *full* file, so a download reassembled
from several parallel range requests can be verified end-to-end. `sha-256` and
`sha-512` are offered (the client's `Want-Repr-Digest` preference picks). Because the
digest requires reading the whole file, it costs nothing on the default download path
(no header, no hashing).

## Caching

```bash
servery --cache 3600        # Cache-Control: max-age=3600
```

By default file responses are `Cache-Control: no-cache` (revalidate every time,
using the strong `ETag`). `--cache SECONDS` sets an explicit `max-age` for serving
static assets that don't change often. Conditional requests
(`If-None-Match`/`If-Modified-Since` → `304`) work either way.

## CORS

```bash
servery --cors
```

Sends permissive CORS headers (`Access-Control-Allow-Origin: *`) and answers
preflight `OPTIONS` — handy when a separate front-end origin needs to fetch these
files.

## Security headers

By default servery sends `X-Content-Type-Options: nosniff` on everything, a scoped
`Content-Security-Policy` + `Referrer-Policy` on its **own generated pages** (the
listing and error pages, never your files), and HSTS over TLS. Turn the defaults off
with `--no-security-headers`.

## Access logging to a file

```bash
servery --access-log access.log
servery --access-log access.log --access-log-format combined
servery --access-log access.log --access-log-format json
```

Writes one line per response to a file, separate from the stderr request log:

| Format | Looks like |
| --- | --- |
| `clf` (default) | Common Log Format — `127.0.0.1 - - [date] "GET /f HTTP/1.1" 200 42` |
| `combined` | CLF + `"referer" "user-agent"` |
| `json` | one JSON object per line (method, path, status, size, …) |

It's thread-safe and records the real status and response size.

## See also

- [Serving files](serving.md) — listings, downloads, archives, SPA.
- [HTTP/2 & HTTP/3](protocols.md) — modern transports + tuning concurrency.

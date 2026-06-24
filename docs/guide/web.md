# Compression, caching & headers

servery applies the cross-cutting web behaviors you'd expect from a real static
server — most on by default, each with a flag.

## On-the-fly gzip

Text-like responses (HTML/CSS/JS/JSON/SVG/XML — and the directory listing itself)
are gzipped when the client sends `Accept-Encoding: gzip`. **On by default**;
disable with `--no-compress`.

It's RFC 9110-correct:

- `gzip` only (deflate is ambiguous), with q-value-aware negotiation.
- `Vary: Accept-Encoding` on every compressible response, and a **distinct
  (`-gz`-suffixed) ETag** for the encoded representation.
- Mutually exclusive with `Range`: a range request is served identity, since a byte
  range over gzipped bytes is incoherent.
- Already-compressed media (jpeg/png/mp4/zip/woff2/…) is never touched — preserving
  the zero-copy `sendfile` fast path.

A typical directory listing compresses ~18×. Compression is applied across HTTP/1.1,
HTTP/2, and HTTP/3.

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

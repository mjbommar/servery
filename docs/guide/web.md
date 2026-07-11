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

Compression is intentionally a bounded policy rather than "always stream" or
"always `read()`": `--max-compress-size` (10 MiB) caps one in-memory static-file
compression, while HTTP/2/3 also require the file to fit
`--max-buffered-response`. Larger files stay identity and stream efficiently. A
byte-bounded `--compression-cache-size` can reuse hot encoded representations; it
is off by default to preserve the minimal-memory posture, while the `cdn` profile
sets 32 MiB. Cache keys include canonical path, mtime, size, coding, and level, and
concurrent misses are coalesced.
Coalescing is transient and also applies when the retained byte budget is zero:
concurrent requests for one representation share the in-flight result, but no
encoded bytes remain after those callers finish. Different keys are not globally
serialized.

## Small-file delivery

Plain HTTP/1 uses two size-dependent paths. Files at or below 16 KiB are read into
one bounded buffer and sent with one socket write; larger files retain the
zero-copy `sendfile` path. The crossover is configurable because a memory-limited
origin may prefer streaming even when it costs some small-response throughput:

```bash
servery --small-file-buffer-size 0       # always sendfile/stream
servery --small-file-buffer-size 16384   # measured default
```

The limit applies per active plaintext HTTP/1 response. It does not make large
files buffered, and it does not replace the separate HTTP/2/3
`--max-buffered-response` policy. TLS already requires bounded userspace copying,
so this particular sendfile crossover does not apply there.

## HTTP/1 connection reuse

By default, a valid persistent HTTP/1 connection may serve any number of requests
until the client closes it or the socket timeout fires. Long-running origins can
put a generous bound on reuse:

```bash
servery --max-requests-per-connection 1000
servery --max-requests-per-connection 0      # unlimited (general default)
servery --keepalive-timeout 10               # shorter idle reuse window
servery --request-head-timeout 30            # total request-line + fields window
servery --request-body-timeout 300           # total body-consumption window
servery --write-timeout 30                   # abort a stalled response writer
```

The final response advertises `Connection: close`; a later pipelined request is
not dispatched. The `cdn` and `app` profiles select 1,000 while other profiles
keep the unlimited default. Lower values recycle connections more often but add
TCP and, for HTTPS, TLS handshake work. This is a request-count policy, not a
slow-client defense: `--timeout`, body limits, `--request-head-timeout`,
`--request-body-timeout`, and `--write-timeout` address different resources.

`--keepalive-timeout` is independent of the count. It bounds an idle persistent
HTTP/1 connection between responses and the next request; when omitted it inherits
the existing 30-second `--timeout`. A shorter value releases connection slots,
threads, tasks, and file descriptors sooner, but clients that pause longer must
reconnect. No profile shortens it yet because origin traffic patterns and TLS
handshake costs differ materially.

`--request-head-timeout` is an opt-in total HTTP/1 budget from the first byte of
the request line through the terminating blank line. It does not reset as a
slow client dribbles fields. The keep-alive idle clock ends at that first byte;
the shorter active/total head budget then applies. Leave it unset to avoid the
measurable first-byte/timer bookkeeping and tolerate unrestricted slow/large
heads, or size it for the slowest legitimate cookies, proxies, and links.
Expiry closes the incomplete connection.

`--request-body-timeout` is an opt-in total HTTP/1 budget. Its clock starts at
the first nonempty body read and spans byte progress plus application pauses
between reads. It complements the per-operation `--timeout`: a client cannot
retain an upload/WSGI/ASGI/proxy/WebDAV body indefinitely merely by sending a
small amount before each progress deadline. Leave it unset for unrestricted
large/slow uploads, or size it for the slowest legitimate body and application
processing cadence. Expiry closes the partially received connection.

`--write-timeout` is opt-in and cross-transport. It bounds how long a socket
write, asyncio drain, or HTTP/3 capacity wait may remain stalled, then aborts the
connection or stream. Progress resets the deadline, so it does not impose a
maximum download duration or minimum bandwidth. Leaving it unset avoids timer
bookkeeping on hot ASGI responses; choose it for exposed origins based on the
slowest legitimate clients and links you intend to support.

## Large directory policy

Pagination bounds rendered HTML, and three separate limits bound the work before
rendering: `--max-listing-entries` (100,000), `--listing-page-size` (1,000), and
`--listing-details-threshold` (10,000). Above the detail threshold servery keeps
basic name/type rows but omits per-entry size/date work, facets, aggregate metrics,
and the timeline. A capped listing says so visibly; it never pretends to be complete.

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

Synchronous writing remains the default because the first production-shaped
bounded-writer benchmark did not pass the protected p99 gate. Set
`--access-log-queue 256` to opt into one server-owned bounded writer thread with
an 8 MiB retained-record budget and batches of up to eight lines. Its default
`block` overflow policy preserves records through backpressure. For a service
where a slow log disk must not stall responses, `--access-log-overflow drop`
preserves request progress but can lose substantial records under saturation;
drops are counted and summarized at shutdown. Queue size, byte budget, batching,
overload, and finite shutdown drain are separately configurable.

CLF and combined fields escape quotes, backslashes, and control characters so a
request cannot inject a second log line. Each server instance owns its own file
handler and writer. Multiworker file logging remains rejected until the parent can
own a single bounded aggregation channel. WSGI, ASGI, CGI/proxy, HTTP/2, and HTTP/3
do not yet share this file-access hook, so do not treat the current file as a
complete all-transport audit log.

## See also

- [Serving files](serving.md) — listings, downloads, archives, SPA.
- [HTTP/2 & HTTP/3](protocols.md) — modern transports + tuning concurrency.

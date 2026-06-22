# servery — Modern Implementation Best Practices (2026)

> Companion to `PRINCIPLES.md`, `ARCHITECTURE.md`, and `REQUIREMENTS.md`. This
> document distills 2026-era best practices from the strongest modern Python HTTP
> code (Starlette's `StaticFiles`/`FileResponse`, httpx) and the CPython standard
> library, and translates each into a **concrete, zero-dependency** recommendation
> for servery.
>
> **Supreme constraint (outranks everything here):** ZERO third-party
> dependencies — pure Python standard library only, forever (`PRINCIPLES.md` §0).
> Every recommendation below is reachable with the stdlib alone, or it is
> explicitly listed in §9 as a boundary we will not cross.

**Conventions.** "Base" = `http.server.SimpleHTTPRequestHandler` /
`BaseHTTPRequestHandler` and `socketserver` that servery extends. Line citations
are against the CPython tree at `/home/mjbommar/src/cpython` (3.14 working copy;
servery's floor is **3.13**, noted where it matters). Starlette/httpx citations
are against the freshly-cloned `servery-refs/`.

A recurring tension worth naming up front: servery **subclasses** the stdlib base
(`ARCHITECTURE.md` §1), while Starlette **owns its whole response pipeline** as an
ASGI app. So we cannot copy Starlette's code; we copy its *decisions* and
re-express them inside the `send_head` choke-point.

---

## 1. HTTP correctness & ergonomics

### 1.1 Default to HTTP/1.1 + keep-alive

**Principle.** A 2026 file server should reuse TCP connections. A directory
listing page pulls many small assets; one connection per asset is the slowest
possible path and defeats TLS session reuse.

**The gap (stdlib).** `BaseHTTPRequestHandler.protocol_version = "HTTP/1.0"`
(`Lib/http/server.py:710`). Under HTTP/1.0 the keep-alive machinery is gated off:
the version comparison `self.protocol_version >= "HTTP/1.1"` at
`server.py:399-401` never fires, so `close_connection` stays `True` and every
request gets a fresh connection. The plumbing for persistent connections already
exists (`handle_one_request` loops `while not self.close_connection`,
`server.py:471`; `Connection:` directive parsing at `server.py:396-401`) — it is
simply disabled by the version floor.

**How the moderns do it.** Starlette/uvicorn speak HTTP/1.1 with keep-alive by
default; correctness depends entirely on **every response being correctly framed**
(exact `Content-Length` or chunked `Transfer-Encoding`). Starlette's `Response`
always populates `content-length` (`responses.py:67-73`) and `FileResponse` sets
it from `stat_result.st_size` (`responses.py:331,336`).

**Zero-dep servery recommendation.**
- Set `protocol_version = "HTTP/1.1"` on `ServeryHandler`. This is a one-line,
  fully stdlib change that flips keep-alive on via the existing base logic.
- **Caveat — framing is now load-bearing.** With HTTP/1.1 the connection is
  reused, so a response with a wrong/missing `Content-Length` corrupts the *next*
  request on the same socket. Audit every code path that writes a body:
  - Files / ranges: always send exact `Content-Length` (the base already does for
    `200` at `server.py:856`; servery's `206` path must send the slice length —
    `REQUIREMENTS.md` FR-RANGE-02).
  - Listings / error pages: send `Content-Length` of the encoded body (base does
    this at `server.py:932` for listings and `server.py:521` for error bodies).
  - **Archives and any streamed body with unknown length** (`tar|gz`): you cannot
    set `Content-Length`. Either emit `Transfer-Encoding: chunked` (frame each
    write yourself — the base does *not* chunk for you) **or** set
    `Connection: close` and close the socket to delimit the body. The simplest
    correct choice for servery's streaming archive is **`Connection: close`** per
    archive response (`send_header("Connection","close")` flips `close_connection`
    via `server.py:568`), accepting the loss of reuse for that one response.
- Honor `Connection: close` from HTTP/1.0 clients (the base already does).
- **Feasibility:** trivial and high-value. The only real work is the framing
  audit, which servery must do for Range correctness anyway.

### 1.2 ETag + conditional GET (mine Starlette)

**Principle.** Serve `304 Not Modified` cheaply on repeat visits. Validators
should cover both date (`If-Modified-Since`) and content (`If-None-Match`).

**The gap (stdlib).** The base honors only `If-Modified-Since` →
`Last-Modified`/`304` (in `send_head`); it emits **no `ETag`** and ignores
`If-None-Match`. `REQUIREMENTS.md` FR-CACHE-02 marks ETag OPTIONAL.

**How Starlette does it.** A **weak-ish strong ETag derived from mtime + size**,
computed once from the `stat` result:

```python
# starlette/responses.py:333-338  (FileResponse.set_stat_headers)
etag_base = str(stat_result.st_mtime) + "-" + str(stat_result.st_size)
etag = f'"{hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
self.headers.setdefault("last-modified", formatdate(stat_result.st_mtime, usegmt=True))
self.headers.setdefault("etag", etag)
```

Conditional match handles the comma list and strips the weak `W/` prefix:

```python
# starlette/staticfiles.py:210-213  (is_not_modified)
etag = response_headers["etag"]
return etag in [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
```

and the `304` body strips to only cache-relevant headers
(`NotModifiedResponse.NOT_MODIFIED_HEADERS`, `staticfiles.py:22-36`).

**Zero-dep servery recommendation.**
- In `httputil.py`, compute the ETag from the `os.stat` servery already does:
  `etag = '"' + hashlib.md5(f"{st.st_mtime_ns}-{st.st_size}".encode(),
  usedforsecurity=False).hexdigest() + '"'`. Use `st_mtime_ns` (finer than
  Starlette's float `st_mtime`) for fewer false matches. `hashlib` is stdlib;
  `usedforsecurity=False` documents that this is not a security hash.
- In `send_head`, after computing the ETag: if `If-None-Match` contains it
  (parse with the `split(",")` + `strip()` + `removeprefix("W/")` idiom above),
  send `304` with only `ETag`, `Last-Modified`, `Cache-Control`, `Date` — drop
  `Content-Length`/`Content-Type`. Keep the base's `If-Modified-Since` path too;
  `If-None-Match` takes precedence when both are present (Starlette's order).
- **Caveat:** mtime+size can miss a same-size edit within the same second only if
  you used seconds; `st_mtime_ns` avoids that. ETags must change across servers
  only if the inode metadata differs — fine for a single-host dev tool.

### 1.3 `filename*` Content-Disposition (RFC 6266/5987)

**Principle.** Download filenames with non-ASCII or special characters must be
transmitted with the RFC 5987 `filename*=UTF-8''…` extended form, not a raw
`filename="…"` (which is latin-1-only and breaks on Unicode).

**The gap (stdlib).** The base never sends `Content-Disposition` for file
downloads at all. servery needs it for the archive feature
(`REQUIREMENTS.md` FR-ARCHIVE-01) and may want it for forced downloads.

**How Starlette does it** — pick ASCII-quoted vs extended based on whether
percent-encoding changed the string:

```python
# starlette/responses.py:319-325
content_disposition_filename = quote(self.filename)
if content_disposition_filename != self.filename:
    content_disposition = f"{type}; filename*=utf-8''{content_disposition_filename}"
else:
    content_disposition = f'{type}; filename="{self.filename}"'
```

**Zero-dep servery recommendation.** Port that exact branch into `httputil.py`
using `urllib.parse.quote`. For the archive name (`sub.tar.gz`), the dir basename
may be non-ASCII, so the extended form matters. Emit **both** a quoted ASCII
fallback and `filename*` for maximum client compatibility:
`attachment; filename="fallback.zip"; filename*=UTF-8''<quoted>`. Sanitize the
basename first (strip CR/LF/`"`) to avoid header injection.

### 1.4 Correct `HEAD`

**Principle.** `HEAD` must return byte-identical headers to `GET` (same
`Content-Length`, `Content-Type`, `ETag`, `Content-Range` on a ranged HEAD) with
an empty body.

**The gap (stdlib).** The base is actually correct here — `do_HEAD`/`do_GET` both
call `send_head`, which sends headers, and only `do_GET` copies the body. The
gap is that servery's *new* code (Range `206`, listings) must preserve this:
emit the full header set, then skip the body when `command == "HEAD"`.

**How Starlette does it.** `FileResponse` computes
`send_header_only = scope["method"].upper() == "HEAD"` (`responses.py:342`) and
sends an empty body for every variant — including the `206` single-range path
(`responses.py:405-406`) and `416`. `REQUIREMENTS.md` FR-RANGE-06 requires the
same.

**Zero-dep servery recommendation.** In `send_head`, compute headers (including
`Content-Range`/`206`/`416`) unconditionally; gate only the body copy on
`self.command != "HEAD"`. Add a regression test: `HEAD` with
`Range: bytes=0-99` returns `206` + `Content-Range` + empty body
(FR-RANGE-06).

---

## 2. Performance / zero-copy

### 2.1 `socket.sendfile()` / `os.sendfile()` for file bodies

**Principle.** A file server's hottest path is "copy a file to a socket." The
kernel can do this without bouncing bytes through userspace.

**The gap (stdlib).** `SimpleHTTPRequestHandler.copyfile` uses
`shutil.copyfileobj(source, outputfile)` (`Lib/http/server.py:967,981`) — a
userspace read/write loop, no zero-copy, and (worse) an **unbounded default
buffer relative to need**; it never calls `sendfile`.

**The stdlib gives us zero-copy for free.** `socket.socket.sendfile(file,
offset, count)` (`Lib/socket.py:483`) already does the right thing: it tries
`os.sendfile` and **transparently falls back** to a send loop when sendfile is
unavailable — its own docstring says *"If os.sendfile() is not available
(e.g. Windows) or file is not a regular file socket.send() will be used
instead"* (`socket.py:490-491`), implemented as the
`_sendfile_use_sendfile` → except `_GiveupOnSendfile` → `_sendfile_use_send`
path (`socket.py:502-505`). `os.sendfile` is present on Linux/macOS
(`hasattr(os, "sendfile")` is True here).

**How Starlette does it.** ASGI servers expose a `http.response.pathsend`
extension and Starlette uses it when available (`responses.py:343,388-389`),
delegating the file→socket copy to the server's zero-copy path; otherwise it
streams in `chunk_size = 64 * 1024` blocks (`responses.py:297,394-396`).

**Zero-dep servery recommendation.**
- Override `copyfile` to attempt `self.connection.sendfile(source)` when:
  (a) the underlying object is a real socket exposing `sendfile`, and
  (b) `source` is a real file (has a usable `fileno()`). Fall back to a
  **bounded** `shutil.copyfileobj(source, outputfile, length=64*1024)`.
- **TLS caveat (critical).** `ssl.SSLSocket` does **not** support `os.sendfile`
  (encryption must happen in userspace). Under HTTPS, `self.connection` is an
  `SSLSocket`; calling `.sendfile()` on it raises and would need the fallback.
  Cleanest rule: **only take the sendfile path when `not isinstance(self.connection,
  ssl.SSLSocket)`**, otherwise go straight to the buffered copy. (`socket.sendfile`
  would itself give up and fall back, but `SSLSocket` overrides `sendfile`, so be
  explicit rather than relying on the exception path.)
- **Range caveat.** Zero-copy with a byte range needs the `count`/`offset`
  arguments: `self.connection.sendfile(f, offset=start, count=length)`. If you
  prefer one code path, keep the bounded `f.seek(start)` + chunked
  `wfile.write` loop already specced in `ARCHITECTURE.md` §6 for `206`, and use
  `sendfile` only for the full-file `200` path. Start there; it is the 90% case.

**Sketch:**

```python
def copyfile(self, source, outputfile):
    sock = self.connection
    if (not isinstance(sock, ssl.SSLSocket)
            and hasattr(sock, "sendfile")
            and hasattr(source, "fileno")):
        try:
            sock.sendfile(source)          # os.sendfile under the hood; falls back internally
            return
        except (OSError, ValueError):
            pass                           # non-regular file, etc. -> buffered copy
    shutil.copyfileobj(source, outputfile, length=64 * 1024)
```

### 2.2 Stream, never buffer; sensible buffer sizes

**Principle.** Peak memory must be O(buffer), not O(file) or O(tree)
(`REQUIREMENTS.md` NFR-PERF-02).

**The gap.** None in the base for plain files (it streams), but servery's *new*
features (listing, archive, upload, range) each introduce a buffering temptation.

**How Starlette does it.** Fixed `chunk_size = 64 * 1024` everywhere
(`responses.py:297`); files opened and read in chunks (`responses.py:391-396`);
multi-range streamed part-by-part (`responses.py:436-444`).

**Zero-dep servery recommendation.**
- Standardize on a **64 KiB** copy buffer (`shutil.copyfileobj(..., length=65536)`)
  — matches Starlette, comfortably above a TCP segment, below cache-pressure.
- **Listings:** build the HTML in memory (it is bounded by entry count, fine) but
  iterate entries with `os.scandir` (lazy), never `os.listdir` + per-entry
  `os.stat` (`DirEntry.stat()` caches; `REQUIREMENTS.md` FR-LIST-06).
- **Archives:** `tarfile.open(fileobj=self.wfile, mode="w|gz")` is genuinely
  streaming (`|` mode never seeks); for zip, stream chunked writes. No
  `Content-Length` → chunked or `Connection: close` (see §1.1).
- **Upload:** stream each multipart part straight to a `tempfile` (`ARCHITECTURE.md`
  §6), enforcing the running byte cap during the read, never buffering the body.

---

## 3. Security (2026 web-facing defaults)

This is the highest-stakes section: **servery renders HTML listings containing
user-controlled filenames** (`REQUIREMENTS.md` FR-LIST-03), so it is an XSS sink
by construction.

### 3.1 Output escaping — the base's escaping is necessary but not sufficient

**Principle.** Every byte of attacker-influenced data placed in HTML must be
context-correctly escaped: element text *and* attribute values *and* URL
components.

**The gap (stdlib).** Two subtle weaknesses in the base's `list_directory`:
1. It escapes with **`html.escape(displayname, quote=False)`**
   (`Lib/http/server.py:924`) — `quote=False` means **`"` and `'` are NOT
   escaped**. That is safe only because the filename is placed in element text
   (`<a ...>NAME</a>`), not in an attribute. The moment servery puts a filename
   into an attribute (e.g. `title="<exact bytes>"`, a `data-name` for the
   client-side filter in FR-SORT-04, or `download="NAME"`), `quote=False`
   becomes an attribute-injection hole.
2. The `_control_char_table` (`server.py:637-639`) that neutralizes control
   characters is applied **only to logging and the request line**
   (`server.py:602,660`) — **not** to filenames in the listing. Raw control
   characters in filenames flow into the page.

   Historically `http.server` has had exactly these classes of bug (CRLF/control
   injection in logs and the open-redirect closed by the `//`→`/` rewrite in
   `parse_request`, gh-87389; `ARCHITECTURE.md` §1).

**How Starlette does it.** Starlette's `StaticFiles` doesn't render listings at
all (no XSS surface) — it 404s a directory without `index.html`. That is itself a
lesson: *the safest listing is no listing*. Where it does build URLs it uses
`urllib.parse.quote` with an explicit `safe` set (`responses.py:213`,
`responses.py:320`).

**Zero-dep servery recommendation (this is the load-bearing one).**
- In `listing.py` / `_templates.py`, escape with **`html.escape(name)`** (default
  `quote=True`) for **everything**, and use it unconditionally for any value that
  could land in an attribute. Do not inherit the base's `quote=False`.
- **Strip/encode control characters in filenames** before rendering. Reuse the
  base's table as a model: build `str.maketrans({c: '' for c in
  range(0x20)} )` (or render as `\xNN` like `server.py:637`) and `.translate()`
  display names. A filename is not allowed to carry raw `\r`, `\n`, `\x00`, or C1
  controls into the page.
- **URL targets** (the `href`) get `urllib.parse.quote(name)`; **display text**
  gets `html.escape`. Two different encodings for two different contexts — never
  cross them (`REQUIREMENTS.md` FR-LIST-03).
- Add an XSS regression test with a file literally named
  `"><img src=x onerror=alert(1)>.txt` and one containing `\r\n` and `\x00`
  (FR-LIST-03), asserting no unescaped markup and no raw control bytes in the body.

### 3.2 Security response headers for generated pages

**Principle.** Defense-in-depth headers cost nothing and blunt whole bug classes.

**The gap (stdlib).** The base sends **none** of these.

**Zero-dep servery recommendation** (all are literally `send_header` calls; no
dep). Apply to *generated HTML* (listings, error pages) at minimum, and ideally
to all responses:

- **`X-Content-Type-Options: nosniff`** — on every response. Stops MIME-sniffing
  a `text/plain` upload into `text/html`. Free, no downside.
- **`Content-Security-Policy`** — restrictive policy for servery's own pages. The
  listing UI is fully self-contained (inline CSS + a small inline filter script,
  `ARCHITECTURE.md` §2 `_templates.py`), so a tight CSP is feasible:
  `default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src
  'unsafe-inline'; form-action 'self'`. `'unsafe-inline'` is needed because the
  filter script/CSS ship inline (zero-dep, no asset pipeline). A nonce-based CSP
  is a stretch goal: generate a per-response nonce with `secrets.token_urlsafe`
  and stamp it on the inline `<script>`/`<style>` — removes `'unsafe-inline'` for
  scripts. **Caveat:** CSP on *served user files* would break legitimate HTML the
  user is hosting, so scope CSP to **servery-generated pages only**, not arbitrary
  served `.html`.
- **`Referrer-Policy: no-referrer`** (or `strict-origin-when-cross-origin`) — keep
  local paths out of `Referer` on outbound links.
- **HSTS (`Strict-Transport-Security`)** — **only under TLS**. Send
  `max-age=63072000; includeSubDomains` *only when `config.tls`*; never on plain
  HTTP (HSTS over HTTP is ignored by spec and meaningless). Keep `preload` off by
  default (it is a near-irreversible commitment, wrong for a dev tool).
- These ride the existing `extra_response_headers` hook (`ARCHITECTURE.md` §1) and
  the `-H/--header` escape hatch (`REQUIREMENTS.md` FR-HDR-01), but the safe
  defaults above should be **on by default for generated pages**, not opt-in.

### 3.3 Path traversal + symlink escape (copy Starlette's `lookup_path`)

**Principle.** No request, symlink, or encoded trick may resolve outside the
served root.

**The gap (stdlib).** The base `translate_path` is good (strips query/fragment,
`posixpath.normpath`, drops `..` and drive letters, retains the `//`
open-redirect fix) **but performs no symlink-escape check** — a symlink *inside*
the root pointing *outside* it is followed.

**How Starlette does it** — `realpath` both sides, then `commonpath` containment,
with absolute-path and null-byte rejection. This is the single most copyable
security routine in the references:

```python
# starlette/staticfiles.py:154-173  (lookup_path)
if path.startswith(("/", "\\")):          # reject absolute -> cannot escape
    return "", None
...
full_path = os.path.realpath(joined_path)  # collapse symlinks
directory = os.path.realpath(directory)
if os.path.commonpath([full_path, directory]) != str(directory):
    continue                               # client tried to break out -> not found
```

and the *caller* maps exceptions to fail-closed statuses:
`PermissionError → 401`, `ENAMETOOLONG → 404`, `ValueError (null bytes) → 404`
(`staticfiles.py:118-128`).

**Zero-dep servery recommendation.**
- `security.resolve` already composes base `translate_path` + `realpath`
  containment (`ARCHITECTURE.md` §5.1). **Prefer `os.path.commonpath([real, root])
  == root`** over the string `startswith(root + os.sep)` check — `commonpath` is
  the cross-platform, separator-correct comparison Starlette uses and avoids the
  `/a/rootEVIL` vs `/a/root` prefix-collision class of bug.
- Reject absolute paths and backslash-absolute (`path.startswith(("/","\\"))`)
  and **catch `ValueError` from embedded NUL bytes** → 404, mirroring
  `staticfiles.py:126-128`.
- **Symlink policy:** default deny-escape via `realpath` (matches
  `follow_symlink=False`, `staticfiles.py:164-165`); `--follow-symlinks` switches
  to `abspath` (matches `follow_symlink=True`, `staticfiles.py:161-162`).
  servery's default (`REQUIREMENTS.md` FR-SERVE-07) already matches Starlette's.

### 3.4 Fail closed; never leak existence/paths

**Principle.** Error responses must not reveal whether a path exists, its real
filesystem location, or why access failed.

**The gap (stdlib).** The base's `send_error` echoes the request path into the
HTML error body — but escapes it with `html.escape(quote=False)`
(`server.py:516-517`), so it is XSS-safe; the leak concern is *information*, not
injection.

**Zero-dep servery recommendation.**
- Traversal/symlink-escape/permission failures all return **`404`, never `403`**
  (`ARCHITECTURE.md` §5.1) — do not distinguish "forbidden" from "missing".
- Do not put resolved filesystem paths in error bodies; the URL path is fine
  (already escaped by the base). Map `PermissionError` to `404` (a dev tool need
  not advertise that a file exists-but-unreadable), diverging from Starlette's
  `401` (`staticfiles.py:118-119`) because servery's auth is page-level, not
  per-file.

### 3.5 Upload bounds & decompression-bomb awareness

**Principle.** Writes are the highest-risk surface; bound them, and never trust
client-declared sizes.

**Zero-dep servery recommendation** (already in `ARCHITECTURE.md` §5.4 — restated
as best practice):
- **Running** byte cap enforced *while streaming* (`config.max_upload`, default
  100 MiB per `REQUIREMENTS.md` DEC-UPLOAD-CAP), not just the spoofable
  `Content-Length`. Abort + delete temp file on overrun → `413`.
- Filenames reduced to `os.path.basename`, re-validated through `security.resolve`;
  write to `tempfile` then `os.replace` (atomic).
- **Decompression-bomb awareness (forward-looking).** If servery ever accepts
  `Content-Encoding: gzip` request bodies or adds response compression
  (§9), enforce an *output* size cap during inflate — `zlib.decompressobj` with a
  bounded `decompress(data, max_length)` loop, never an unbounded
  `gzip.decompress(blob)`. A 1 KB gzip can inflate to gigabytes. (Not in v1, but
  recorded so the upload parser is not retrofitted carelessly.)

---

## 4. TLS best practices

**Principle.** Use the stdlib's modern, secure-by-default TLS context; do not
hand-roll cipher strings or call deprecated APIs.

**The gap (stdlib).** None — `http.server.HTTPSServer`/`ThreadingHTTPSServer`
already build the right context (`ssl.create_default_context(Purpose.CLIENT_AUTH)`
+ `load_cert_chain` + ALPN `["http/1.1"]`, per `ARCHITECTURE.md` §1). servery's
job is to *not regress* this.

**Zero-dep servery recommendation.**
- Build the context with **`ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)`**
  and `ctx.load_cert_chain(cert, key, password=...)` (`REQUIREMENTS.md` FR-TLS-01).
  This already disables SSLv2/3, TLS 1.0/1.1, compression (CRIME), and weak
  ciphers.
- **Minimum version:** `ctx.minimum_version = ssl.TLSVersion.TLSv1_2` (prefer 1.3
  when negotiable — leave `maximum_version` unset so 1.3 is used when both peers
  support it). Do **not** force `TLSv1_3` minimum: it would reject otherwise-fine
  1.2-only clients with no security benefit for a LAN tool.
- **ALPN:** `ctx.set_alpn_protocols(["http/1.1"])` — advertise only what we speak
  (no HTTP/2; §9).
- **Ciphers:** do **NOT** call `ctx.set_ciphers(...)` with a hand-written string.
  The stdlib default suite tracks OpenSSL's vetted defaults and updates with the
  interpreter; a hand-rolled string rots into insecurity. Override only if a
  concrete compliance requirement forces it, and document why.
- **Session handling:** leave stdlib defaults (session tickets on); don't disable.
- **Passphrase:** read from a **file**, never a CLI arg (`REQUIREMENTS.md`
  FR-TLS-02 / NFR-SEC-02) — avoids process-list exposure.
- **Optional mTLS:** `ctx.load_verify_locations(cafile=...)` +
  `ctx.verify_mode = ssl.CERT_REQUIRED` when `--tls-client-ca` is set
  (`REQUIREMENTS.md` FR-TLS-04); off by default.
- **What NOT to do:** never `ssl.wrap_socket` (removed/deprecated); never
  `check_hostname=False` on a server context; never ship a cert/key in the repo.

---

## 5. Concurrency & robustness

### 5.1 Default socket timeout (Slowloris)

**Principle.** A client that opens a connection and dribbles bytes (or none) must
not pin a worker forever.

**The gap (stdlib).** `socketserver.BaseRequestHandler`/`BaseServer` has
`timeout = None` by default (`Lib/socketserver.py:198,800`). With
`ThreadingMixIn` and `daemon_threads`, each slow client holds a thread
indefinitely — textbook Slowloris exposure. The machinery exists:
`StreamRequestHandler` applies `self.connection.settimeout(self.timeout)` *if
timeout is set* (`socketserver.py:808-809`), but nothing sets it.

**Zero-dep servery recommendation.**
- Set a **per-request socket timeout** on `ServeryHandler`: define `timeout = 30`
  (seconds) on the handler class. `StreamRequestHandler.setup` will then call
  `settimeout` (`socketserver.py:808`), so a stalled read/write raises
  `TimeoutError` instead of hanging. Tune via a `Config.timeout` field.
- **Caveat:** a *legitimate* very slow large-download client could trip a short
  read/write timeout. 30s of inactivity is a reasonable default; the timeout is
  per-`recv`/`send`, not per-request-total, so it tolerates slow-but-steady
  transfers. Document it and make it configurable; allow `0`/`None` to disable for
  power users on trusted LANs.

### 5.2 Bounded concurrency vs unbounded threads

**Principle.** Unbounded thread-per-connection is a DoS amplifier: N connections
→ N threads → memory/FD exhaustion.

**The gap (stdlib).** `ThreadingMixIn.process_request` spawns a new
`threading.Thread` per connection with **no cap** (`socketserver.py:687-707`).
`daemon_threads=True` (servery sets it) only affects shutdown, not the count.

**Zero-dep servery recommendation.** Two stdlib-only options, in order of
preference:
1. **A `concurrent.futures.ThreadPoolExecutor` connection cap.** Override
   `process_request` to submit `process_request_thread` to a bounded
   `ThreadPoolExecutor(max_workers=N)`. This caps live request-handlers at `N`;
   excess connections queue. Pure stdlib, ~10 lines, and the bound is a single
   `Config.max_workers`. This is the recommended approach — explicit, simple,
   testable.
2. **A `threading.Semaphore` gate** acquired at the top of `handle` and released
   in `finally`. Simpler but rejects/blocks at handler entry rather than at
   accept; the executor approach composes better with graceful shutdown.

Keep the **default unbounded** (matches the stdlib and `NFR-PERF-01`, and a dev
tool rarely needs the cap) but expose `--max-workers`/`Config.max_workers` so a
network-exposed deployment can bound it. Honest posture: servery is not
production-hardened (`PRINCIPLES.md` §1); the cap is a mitigation, not a promise.

### 5.3 Graceful shutdown

**Zero-dep servery recommendation.** Already correct in `ARCHITECTURE.md` §7's
test fixture: `httpd.shutdown()` + `httpd.server_close()`, daemon serve thread,
`thread.join(timeout=...)`. For the CLI, install a `signal.signal(SIGINT, ...)` /
`try/except KeyboardInterrupt` around `serve_forever()` that calls `shutdown()`
then `server_close()` so sockets are released cleanly and in-flight requests
drain (`ThreadingMixIn` block-on-shutdown joins non-daemon threads;
`socketserver.py:712`).

### 5.4 Swallow expected disconnects without tracebacks

**Principle.** A client closing mid-download is **normal**, not an error. It must
not print a traceback or log at error level.

**The gap (stdlib).** The base's `copyfile` can raise `BrokenPipeError` /
`ConnectionResetError` straight out of `serve_forever`, producing an ugly
traceback on every cancelled download.

**How Starlette does it.** Treats client disconnect as a clean signal —
`StreamingResponse` catches `OSError` on send and raises a tidy `ClientDisconnect`
(`responses.py:268-271`).

**Zero-dep servery recommendation.** Wrap the body-write path (`copyfile`, range
emit, archive stream) in a handler that catches
`(BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)`,
sets `self.close_connection = True`, and returns quietly (optionally a single
debug-level log line — no traceback). Do **not** catch bare `Exception`. This is
a few lines and makes the server feel robust under real browser behavior
(range-seek scrubbing, tab closes).

---

## 6. Observability / logging

### 6.1 Route through `logging`, with a library `NullHandler`

**Principle.** A library must not hijack the root logger or hardcode a sink; the
*application* decides where logs go.

**The gap (stdlib).** The base writes **straight to `sys.stderr`**
(`Lib/http/server.py:671`, inside `log_message`), never touching the `logging`
module. There is no level, no handler, no way for an embedder to redirect or
silence it short of overriding the method.

**Zero-dep servery recommendation.**
- Create a module logger `logger = logging.getLogger("servery")` and add a
  **`logging.NullHandler()`** to it at import (the canonical library pattern):
  servery emits no output unless the embedding application configures handlers.
- Override `log_message`/`log_request` to call `logger.info(...)` /
  `logger.warning(...)` instead of writing to stderr.
- The **CLI** (not the library) configures a `StreamHandler` to stderr in
  `cli.main()` — so `python -m servery` still prints request lines (preserving
  `REQUIREMENTS.md` FR-LOG-01) and TTY colorization, while `import servery` stays
  silent until the embedder opts in (`NFR-API-01`). This is the clean split:
  *library is quiet, CLI is loud.*

### 6.2 Capture status AND byte count

**Principle.** Access logs need the response status and the bytes actually sent.

**The gap (stdlib).** `log_request(code, size)` is called from `send_response`
with **`size='-'`** by default (`server.py:610-619`) — the base **cannot report
the byte count** because `send_response` runs *before* the body is written, and
nothing threads the final byte total back. So stdlib access logs always show `-`
for size.

**Zero-dep servery recommendation.**
- Track bytes in the handler: wrap `self.wfile` writes (or accumulate in
  `copyfile`/range/archive emitters) into `self._bytes_sent`, and log it in a
  `finish()`/post-body hook rather than relying on `send_response`'s premature
  call. The base now stashes `self._log_request_info = (code, size)`
  (`server.py:616`) — servery can update a byte counter and emit the real total
  at end-of-request.
- Emit **Common Log Format** (`host - - [time] "request" status bytes`) or
  **Combined** (adds `Referer`/`User-Agent`) as an **opt-in** access-log mode
  (a `Config.access_log` format flag), defaulting to the human-friendly line for
  dev use.

### 6.3 Optional request IDs

**Zero-dep servery recommendation.** Generate a short id per request with
`secrets.token_hex(8)` (stdlib), stash on the handler, include it in log lines and
optionally echo an `X-Request-ID` response header. Off by default; one small
flag. Useful when servery sits behind a proxy.

---

## 7. Code quality & API design (2026 Python)

**Principle.** 3.13+ lets us write clean, fully-typed, mypy/pyright-clean code
with no compatibility cruft. The base class has **no type hints** and uses
`%`-formatting throughout — servery should not inherit that style in its own code.

**Zero-dep servery recommendations.**
- **Full modern type hints.** `X | None` (not `typing.Optional[X]`), builtin
  generics `list[str]`/`dict[str, int]` (not `typing.List`), `collections.abc`
  for protocols. Note Starlette itself uses exactly this 3.10+ syntax
  (`staticfiles.py:42-48`, `responses.py:33-40`). Target **mypy/pyright-clean** as
  a CI gate.
- **Frozen `@dataclass` config as single source of truth.**
  `@dataclass(frozen=True, slots=True)` `Config` (`ARCHITECTURE.md` §2) — immutable,
  hashable, the only thing the CLI produces and the server/handler consume. No
  global mutable state, no reading `argparse` namespaces or env deep in the code.
- **`__all__`** in `__init__.py` enumerating the public surface
  (`serve`, `make_server`, `Config`, `ServeryHandler`) — `NFR-API-01`.
- **Library-first, importable.** `serve(config)` / `make_server(config)` are the
  product; the CLI is a thin view (`PRINCIPLES.md` §4). Provide a
  **context-manager server** so embedders can `with make_server(cfg) as httpd:` —
  `socketserver.BaseServer` already supports `__enter__`/`__exit__` (closes on
  exit), so this is free; just document and test it.
- **f-strings** over `%`/`.format` in servery's own code; `string.Template` for
  the HTML templates (data, not logic — `ARCHITECTURE.md` §2).
- **`pathlib` where it doesn't fight path-safety.** Use `pathlib.Path` for config
  ergonomics (the served root, cert paths). But keep the **security-critical
  containment in `os.path`** (`realpath`/`commonpath`) — Starlette deliberately
  uses `os.path`, not `pathlib`, in `lookup_path` (`staticfiles.py:154-173`),
  because the string-level `commonpath` check is the audited primitive. Do not
  rewrite `security.resolve` in `pathlib` for style points.
- **No metaprogramming for its own sake** (`NFR-QA-02`): the one synthesized class
  in `build_handler` (`ARCHITECTURE.md` §4) is the only acceptable bit, and it is
  composition-by-method-presence, not a mixin tower.

---

## 8. Testing (2026)

**Principle.** Zero-dep extends to the test surface: stdlib **`unittest` only**,
no pytest/hypothesis (`REQUIREMENTS.md` NFR-QA-01).

**Zero-dep servery recommendations** (building on `ARCHITECTURE.md` §7):
- **Real servers on ephemeral ports.** Bind `("127.0.0.1", 0)`, read the actual
  port from `server_address[1]`, run `serve_forever` in a daemon thread, hit it
  with **`http.client.HTTPConnection`** / `urllib.request`. Real sockets, real
  HTTP, no handler mocking. Tear down with `shutdown()` + `server_close()`.
- **Table-driven edge-case fuzzing (no hypothesis).** Since we cannot use
  property-based testing, encode the corpus explicitly:
  - **Range parser:** a table of `(header, file_size) -> expected (status,
    Content-Range, length)` covering `bytes=0-0`, `bytes=-N`, `bytes=N-`,
    `bytes=N-` past EOF, `bytes=abc` (→200), reversed `bytes=10-5`, multi-range,
    whitespace, missing unit. Mirror the cases Starlette's `_parse_ranges`
    handles (`responses.py:495-522`) — empty parts, `-` only, non-numeric — as a
    proven oracle for the corpus.
  - **Multipart parser:** a table of crafted bodies — boundary straddling a read
    chunk seam, missing final boundary, filename with `../`, empty filename,
    duplicate parts, oversize part (→413), CRLF vs LF line endings. Feed bytes
    straight to `upload.parse_multipart` (no server) per `ARCHITECTURE.md` §7.
- **TLS fixtures.** Generate a throwaway cert/key into a `tempfile` via an
  `openssl`-availability-guarded test helper (or ship a tiny fixture cert), build
  a trusting `ssl.SSLContext`, assert an HTTPS round-trip; `skipUnless` when cert
  generation is unavailable (`ARCHITECTURE.md` §7).
- **Path-traversal regression suite (non-negotiable).** `GET /../../etc/passwd`,
  `%2e%2e%2f`, absolute paths, backslash paths, NUL-byte paths, and an in-root
  symlink pointing outside the root → all `404`. Run against **both**
  `security.resolve` directly **and** a live server (FR-SERVE-06/07).
- **XSS/escaping regression** (§3.1): hostile filenames through a live listing.
- **Composition assertions:** a default `Config` yields a handler class with **no**
  `do_POST`/`do_OPTIONS`; `upload=True` yields one with `do_POST`
  (`ARCHITECTURE.md` §7).
- **Command:** `python -m unittest discover` is the entire test runner.

---

## 9. Explicit non-stdlib boundaries

What a 2026 "best-practice" web server would want, but servery **cannot** do
zero-dep — recorded so they are not re-litigated, with the graceful-degradation
plan for each.

| Want | Why it's out (no stdlib path) | servery degradation |
|---|---|---|
| **HTTP/2** | No `hpack` (HPACK header compression) or HTTP/2 framing in the stdlib. | Advertise only `http/1.1` via ALPN (§4); HTTP/1.1 + keep-alive (§1.1) is the ceiling. Put a reverse proxy in front for h2. |
| **HTTP/3 / QUIC** | No QUIC, no `qpack`, no UDP transport stack in the stdlib. | Same: document "front with a proxy" (`NFR-SEC-03`). |
| **Brotli (`br`) response encoding** | No `brotli` in the stdlib. | Negotiate only what *is* stdlib (below). Never advertise `br`. |
| **Publicly-trusted / ACME certs** | The full ACME protocol + long-lived-key crypto + a public domain on :80/:443 is production-web-server territory; would warrant a future `servery[acme]` extra. | Out of scope; user supplies a publicly-trusted cert via `--tls-cert`. (NB: ad-hoc *self-signed* certs ARE zero-dep feasible and shipped — see next row.) |
| **Ad-hoc self-signed TLS certs** | *Has* a zero-dep stdlib path. `ssl` itself has no X.509/keygen API, but pure-Python RSA+DER+PKCS#1 (`_certgen.py`) fills the gap with zero deps. | Shipped via `--tls-self-signed` (`REQUIREMENTS.md` FR-TLS-05) for opportunistic encryption (untrusted by clients); `--tls-help` still prints an `openssl` recipe for the user-cert path. |
| **Markdown / rich rendering** | No stdlib Markdown parser. | Serve README as **escaped plaintext** at most (`PRINCIPLES.md` §0). |
| **QR code for share URL** | No stdlib QR encoder. | Print the URL as text in the startup banner. |

**Compression — what IS stdlib (verified on this tree).** This matters because
response compression is a plausible future feature (`REQUIREMENTS.md` §5 lists it
deferred):
- **`gzip` and `zlib` (deflate)** are stdlib on every supported version
  (verified: `import gzip, zlib` OK; `Lib/gzip.py`, `Lib/zlib.*`). These are the
  two `Content-Encoding` tokens servery may safely negotiate.
- **`bz2`, `lzma`** are stdlib but are **not** valid HTTP `Content-Encoding`
  tokens for browsers — do not advertise them for responses.
- **`zstd`** is stdlib **only from Python 3.14** (`compression.zstd`, PEP 784;
  verified `from compression import zstd` works here on 3.14, and the `compression`
  package — `Lib/compression/` with `gzip`/`bz2`/`lzma`/`zlib`/`zstd` — is the
  3.14 reorg). **servery's floor is 3.13, where `zstd` is absent.** So:
  - If servery ever adds response compression, **negotiate `gzip`/`deflate`
    unconditionally** and gate `zstd` behind a `sys.version_info >= (3, 14)` /
    `try: from compression import zstd` probe — advertise `zstd` in
    `Accept-Encoding` matching **only** when the import succeeds. This degrades
    gracefully: on 3.13 you simply never offer `zstd`.
- The base `http.server` does **no** content negotiation at all (verified: no
  `Accept-Encoding` handling in `Lib/http/server.py`); any compression is
  servery-built. Mind the **decompression-bomb** rule (§3.5) and never compress an
  already-compressed type (images, archives, `.gz`).

---

## Appendix: the one thing to copy from Starlette

If servery copies exactly one routine from the references, copy
**`StaticFiles.lookup_path` (`starlette/staticfiles.py:154-173`)**: the
`realpath`-both-sides + `os.path.commonpath` containment check, plus
absolute-path and null-byte rejection, with the caller mapping each failure to a
*fail-closed* status. It is small, audited, stdlib-only, cross-platform, and it is
the difference between "blocks `../`" (which the base already does) and "a symlink
inside the root cannot point outside it" (which the base does **not**). It is
servery's single highest-stakes piece of code, and Starlette has already written
the correct version of it.

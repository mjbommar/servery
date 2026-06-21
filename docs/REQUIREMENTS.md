# servery — Requirements Specification

> A batteries-included `python -m http.server`.
>
> **Supreme constraint (outranks everything below): ZERO third-party
> dependencies — pure Python standard library only, forever.** Any requirement
> here that could only be satisfied by adding a dependency is, by definition, out
> of scope. See `PRINCIPLES.md` §0.

This document specifies the functional and non-functional requirements for
**servery** v1. It is derived from and subordinate to `VISION.md`,
`PRINCIPLES.md`, and `REFERENCES.md`. Where this document and those disagree,
those win.

Requirements use stable IDs (`FR-<AREA>-NN` for functional, `NFR-<AREA>-NN` for
non-functional). Acceptance criteria are written to be observable and testable
with stdlib `unittest` + an HTTP client (`http.client` / `urllib.request`).

**Seeded decisions honored as decided (not relitigated):** minimum Python 3.13;
servery implements Range itself; user-provided TLS cert/key only; single shared
Basic-Auth credential with optional pre-hashed form; opt-in streamed upload with
bounded size; localhost-by-default bind; no symlink-following out of root by
default. See §6 for the full decision register.

---

## 1. Functional Requirements

### 1.1 Serving & MIME

**FR-SERVE-01 — Serve a directory tree over HTTP.**
servery serves files from a single configured root directory (default: current
working directory) using `GET` and `HEAD`, building on
`http.server.SimpleHTTPRequestHandler`.
*Acceptance:* `GET /file.txt` for an existing file under root returns `200` with
the file bytes and a `Content-Length` equal to the file size; `HEAD` returns the
same status/headers with an empty body.

**FR-SERVE-02 — Correct MIME detection.**
Content types are resolved via `mimetypes.guess_file_type` (path-based; the
3.13+ replacement for the soft-deprecated `guess_type`), with an
`extensions_map` override and a configurable default for unknown extensions.
*Acceptance:* `GET /a.html` yields `Content-Type: text/html`; `GET /a.json`
yields `application/json`; an unknown extension yields the configured default
(`application/octet-stream` unless `--content-type` is set).

**FR-SERVE-03 — Index document serving.**
For a directory request, if an index document exists it is served instead of a
listing. The default index set is `index.html`, `index.htm`; a custom name may be
supplied via `--index`.
*Acceptance:* with `index.html` present, `GET /` returns its contents with
`Content-Type: text/html`, not a directory listing. With `--index home.html` and
`home.html` present, `GET /` serves `home.html`.

**FR-SERVE-04 — Directory redirect normalization.**
A request for a directory without a trailing slash returns `301` to the
slash-suffixed URL (inherited base-class behavior; must be preserved).
*Acceptance:* `GET /sub` where `sub/` is a directory returns `301` with
`Location: /sub/`.

**FR-SERVE-05 — Conditional GET (`If-Modified-Since`).**
servery honors `If-Modified-Since` and returns `304 Not Modified` when the file
is unchanged (inherited; must be preserved). `ETag`/`If-None-Match` is OPTIONAL
(see FR-CACHE-02).
*Acceptance:* a `GET` with `If-Modified-Since` >= the file mtime returns `304`
with no body.

**FR-SERVE-06 — Path-traversal protection (hard).**
No request may resolve to a path outside the served root. Path translation reuses
the security-reviewed base `translate_path` (strips query/fragment,
`posixpath.normpath`, drops `..` and drive components, retains the `//`
open-redirect protection from gh-87389) and MUST NOT be weakened.
*Acceptance:* `GET /../../etc/passwd`, `GET /%2e%2e/%2e%2e/etc/passwd`, and
absolute-path tricks all return `404` (or `403`) and never serve content outside
root. A regression test asserts the resolved path is contained in
`realpath(root)`.

**FR-SERVE-07 — Symlink containment (default deny-escape).**
By default, servery does not serve content via symlinks that resolve outside the
served root. Containment is verified with `os.path.realpath(target)` starting
with `realpath(root)`. A flag `--follow-symlinks` opts into following links that
leave the root (with the understanding that this re-enables a traversal-adjacent
behavior).
*Acceptance:* with a symlink `link -> /etc/passwd` inside root, `GET /link`
returns `404`/`403` by default; with `--follow-symlinks`, it serves the target.
In-root symlinks (pointing to a sibling under root) are served in both modes.

### 1.2 Directory Listing

**FR-LIST-01 — Rich HTML listing.**
For a directory with no applicable index, servery renders an HTML listing
(replacing the base-class plain `<ul>`) as a table with at least: **Name**,
**Size**, **Last Modified (mtime)** columns. Directories are visually
distinguished and sorted before files by default (dirs-first).
*Acceptance:* `GET /` on an index-less directory returns `200`,
`Content-Type: text/html; charset=utf-8`, and an HTML table whose header row
contains "Name", "Size", and "Last modified" cells; directory rows appear before
file rows under the default sort.

**FR-LIST-02 — Human-readable and exact sizes; formatted dates.**
Sizes render human-readable (e.g. `2.0 KiB`) with the exact byte count available
(title attribute or a toggle). Dates render in a stable, sortable,
locale-independent format (ISO-8601 `YYYY-MM-DD HH:MM`).
*Acceptance:* a 2048-byte file shows `2.0 KiB` with the exact `2048` byte value
present in the row markup; mtime renders as ISO-8601 in UTC or local time
consistently.

**FR-LIST-03 — Safe rendering (escaping).**
All file names and metadata are HTML-escaped (`html.escape`) and link targets are
percent-encoded (`urllib.parse.quote`). A filename containing `<`, `&`, or quotes
cannot inject markup.
*Acceptance:* a file literally named `<b>x</b>.txt` appears as escaped text in
the listing; the page contains no unescaped `<b>` originating from the filename.

**FR-LIST-04 — Breadcrumbs / parent navigation.**
The listing shows the current path as navigable breadcrumbs (or at minimum a
working parent-directory link), never allowing navigation above root.
*Acceptance:* `GET /a/b/` shows links for `a/` and the root; following the root
link reaches `/`; there is no link that resolves above root.

**FR-LIST-05 — Hidden-file policy.**
Dotfiles are hidden by default; `--show-hidden` includes them.
*Acceptance:* with a `.secret` file present, `GET /` omits it by default and
includes it under `--show-hidden`. (Hiding is cosmetic, not an access control —
a direct `GET /.secret` still serves the file; this is documented.)

**FR-LIST-06 — Per-entry error resilience.**
`os.scandir` / `DirEntry.stat()` failures (broken symlinks, permission errors)
are caught per entry (`OSError`) so one bad entry cannot break the whole listing.
*Acceptance:* a directory containing a broken symlink still returns `200`; the
broken entry is rendered with placeholder metadata (e.g. `—`) rather than raising.

**FR-LIST-07 — Disable indexing.**
`--no-listing` disables directory listings entirely; directory requests without
an index return `403`/`404`.
*Acceptance:* with `--no-listing`, `GET /` on an index-less directory returns
`403` (or `404`) and no listing body.

### 1.3 Sorting & Search

**FR-SORT-01 — Apache-compatible sort URL scheme (`?C=&O=`).**
Column-header links are self-referencing and encode the **next** sort state using
the Apache `mod_autoindex` query scheme: `C=` in `{N=name, M=mtime, S=size}` and
`O=` in `{A=ascending, D=descending}`. Both `&` and `;` are accepted as argument
separators on input; servery emits `&`.
*Acceptance:* clicking the **Size** header issues `GET /?C=S&O=A`; the response is
sorted by ascending size. `?C=S;O=D` (semicolon form) is accepted and sorts by
descending size.

**FR-SORT-02 — Toggle semantics.**
Clicking the currently-sorted column toggles `O=A`↔`O=D`; clicking a different
column selects it with `O=A`. Dirs-first grouping is preserved across sorts by
default.
*Acceptance:* given current state `?C=S&O=A`, the Size header link targets
`?C=S&O=D`; the Name header link targets `?C=N&O=A`.

**FR-SORT-03 — Default sort order.**
Default is name-ascending, dirs-first. A server-side default may be set via
`--sort` (`name|size|date`) and `--order` (`asc|desc`).
*Acceptance:* `GET /` with no query renders name-ascending dirs-first; with
`--sort size --order desc`, the no-query listing is size-descending.

**FR-SORT-04 — Client-light search/filter.**
The listing includes an inline (shipped-as-text, zero third-party) search box
that filters visible rows client-side without a server round-trip. A server-side
`?q=<substring>` filter is ALSO honored for JS-free clients.
*Acceptance:* typing in the filter box hides non-matching rows without a new
request (verified by inspecting the inline script); `GET /?q=report` returns a
listing containing only entries whose name contains `report`.

**FR-SORT-05 — Optional server-ignore of client sort.**
`--ignore-client-sort` (mirrors Apache `IndexOptions IgnoreClient`) makes the
server ignore `?C=&O=` from clients and always use the server default — a
hardening/determinism toggle.
*Acceptance:* with `--ignore-client-sort`, `GET /?C=S&O=D` is rendered in the
server default order, not size-descending.

### 1.4 Range / Resumable Downloads

> stdlib `http.server` does **not** implement Range (verified — see
> `REFERENCES.md` §0.2). servery implements it itself.

**FR-RANGE-01 — Advertise range support.**
Successful file responses (`200`) include `Accept-Ranges: bytes`.
*Acceptance:* `GET /big.bin` returns `200` with `Accept-Ranges: bytes`.

**FR-RANGE-02 — Single byte-range request → `206`.**
A valid `Range: bytes=a-b` is honored: servery `seek()`s and returns `206 Partial
Content` with `Content-Range: bytes a-b/total` and a `Content-Length` equal to the
returned slice length.
*Acceptance:* for a 1000-byte file, `Range: bytes=0-99` returns `206`,
`Content-Range: bytes 0-99/1000`, `Content-Length: 100`, and exactly the first
100 bytes.

**FR-RANGE-03 — Open-ended and suffix ranges.**
`bytes=a-` (from `a` to end) and `bytes=-N` (final `N` bytes) are supported.
*Acceptance:* for a 1000-byte file, `bytes=500-` returns `206`,
`Content-Range: bytes 500-999/1000`, `Content-Length: 500`; `bytes=-100` returns
`Content-Range: bytes 900-999/1000`, `Content-Length: 100`.

**FR-RANGE-04 — Unsatisfiable range → `416`.**
A range whose start is beyond the file size (or otherwise unsatisfiable) returns
`416 Range Not Satisfiable` with `Content-Range: bytes */total`.
*Acceptance:* for a 1000-byte file, `bytes=2000-3000` returns `416` with
`Content-Range: bytes */1000`.

**FR-RANGE-05 — Malformed / unsupported range falls back to `200`.**
A syntactically invalid `Range` header (or a multi-range request, which is not
required to be supported) is ignored and the full entity is served as `200`.
*Acceptance:* `Range: bytes=abc` returns `200` with the full file. Multi-range
(`bytes=0-9,20-29`) either returns `200` full body OR a correctly-formed
`multipart/byteranges` `206`; single-range support is the only mandatory level.

**FR-RANGE-06 — Range honored on HEAD.**
`HEAD` with a satisfiable `Range` returns the `206`/`416` status and headers with
no body.
*Acceptance:* `HEAD` with `Range: bytes=0-99` on a 1000-byte file returns `206`,
`Content-Range: bytes 0-99/1000`, empty body.

### 1.5 Authentication (Basic, single shared credential)

**FR-AUTH-01 — Single shared Basic credential gate.**
`--auth user:pass` enables HTTP Basic Auth with one shared credential guarding all
requests. Multi-user / accounts / sessions are out of scope for v1.
*Acceptance:* with `--auth alice:s3cret`, a request with no `Authorization`
header returns `401` with `WWW-Authenticate: Basic realm="servery"`; a request
with the correct `Authorization: Basic <b64(alice:s3cret)>` returns `200`.

**FR-AUTH-02 — Pre-hashed credential form.**
The password may be supplied pre-hashed as `user:sha256:<hex>` or
`user:sha512:<hex>` (miniserve-parity raw digests). servery hashes the presented
password with the named algorithm and compares.
*Acceptance:* with `--auth alice:sha256:<sha256('s3cret')>`, presenting
`alice:s3cret` authenticates; presenting a wrong password returns `401`. An
unrecognized algorithm prefix is a startup error.

**FR-AUTH-03 — Constant-time comparison.**
Credential comparison uses `hmac.compare_digest`, never `==`, for both username
and password/digest.
*Acceptance:* code/test asserts `hmac.compare_digest` is used; a unit test feeds
matching and non-matching credentials and confirms accept/reject without `==`.

**FR-AUTH-04 — Loud warning when auth without TLS.**
If `--auth` is enabled and TLS is not, servery prints a prominent startup warning
that credentials travel effectively in the clear over plain HTTP.
*Acceptance:* starting with `--auth` and no `--tls-cert` emits a stderr warning
containing "auth" and "without TLS" (or equivalent); starting with both `--auth`
and TLS emits no such warning.

**FR-AUTH-05 — Auth applies before any side effect.**
Authentication is enforced before upload writes, archive generation, or listing —
no protected action runs for an unauthenticated request.
*Acceptance:* an unauthenticated `POST` to the upload endpoint returns `401` and
writes nothing to disk.

### 1.6 TLS / HTTPS

> User-provided cert/key ONLY. Pure stdlib cannot generate a self-signed cert,
> so servery does **not** promise auto-generation (see §6, DEC-TLS).

**FR-TLS-01 — Serve over HTTPS with provided cert/key.**
`--tls-cert <path>` and `--tls-key <path>` enable HTTPS via an
`ssl.SSLContext` built with `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)`
+ `load_cert_chain`, mirroring `http.server.HTTPSServer` (modern `SSLContext`,
ALPN `["http/1.1"]`; never the deprecated `ssl.wrap_socket`).
*Acceptance:* with a valid cert/key, an `https://` client completes a TLS
handshake and receives served content; an `http://` request to the TLS port fails
to handshake (does not serve plaintext).

**FR-TLS-02 — Encrypted key passphrase.**
An encrypted private key is supported via `--tls-password-file <path>` (the
passphrase is read from a file, not the CLI, to avoid leaking it in process args).
*Acceptance:* an encrypted key plus the correct password file loads and serves;
an incorrect/missing password file produces a clear startup error.

**FR-TLS-03 — Self-signed helper is documentation, not a feature.**
When TLS is requested but cert/key are absent, or via `--tls-help`, servery prints
a ready-to-run `openssl` one-liner for generating a self-signed cert/key. servery
does not generate certs itself.
*Acceptance:* invoking `--tls-help` (or `--tls-cert` without a key) prints a
command of the form
`openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"`
and exits non-zero (or continues per the absent-arg case) without crashing.

**FR-TLS-04 — Optional mutual TLS (client certs) [NICE-TO-HAVE].**
`--tls-client-ca <path>` enables mTLS: `ctx.load_verify_locations(cafile=...)` +
`ctx.verify_mode = ssl.CERT_REQUIRED`, rejecting clients without a CA-signed cert.
This is a v1 nice-to-have; if shipped it must be off unless the flag is given.
*Acceptance (if implemented):* with `--tls-client-ca ca.pem`, a client presenting
a CA-signed cert connects; a client with no/invalid client cert is rejected at the
TLS layer.

### 1.7 Upload (opt-in, streamed, bounded)

> Multipart parsing without `cgi` (removed in 3.13). Stream to disk; never buffer
> the whole body in memory. See §6, DEC-UPLOAD.

**FR-UPLOAD-01 — Upload is opt-in.**
Writing files is disabled unless `--upload` is given. Without it, `POST` to any
path returns `405` (or `403`).
*Acceptance:* without `--upload`, `POST /` returns `405`/`403` and writes nothing.
With `--upload`, `POST` of a valid multipart form stores the file.

**FR-UPLOAD-02 — Multipart parsing without `cgi`, streamed to disk.**
`multipart/form-data` is parsed by hand-rolled boundary splitting off `rfile`
(boundary taken from the `Content-Type` header; per-part headers parsed via
`email.parser`), writing each file part directly to a `tempfile` and then atomic
`os.rename` into place. The whole body is never loaded into memory.
`application/x-www-form-urlencoded` bodies use `urllib.parse.parse_qsl`.
*Acceptance:* uploading a file larger than a configured small buffer succeeds with
peak process memory well below the file size (verified by a streaming test with a
synthetic large part); a partially-written upload that fails mid-stream leaves no
file at the destination (temp file discarded, no partial under the visible name).

**FR-UPLOAD-03 — Bounded maximum size.**
`--max-upload-size <bytes>` caps the accepted upload size; exceeding it aborts the
write and returns `413 Payload Too Large`. Default cap: **100 MiB** (see §6).
*Acceptance:* with `--max-upload-size 1048576`, a 2 MiB upload returns `413` and
leaves no file on disk; a 512 KiB upload succeeds.

**FR-UPLOAD-04 — Filename safety (no traversal).**
Uploaded filenames are sanitized to a basename and verified to resolve within the
upload target; traversal attempts (`../`, absolute paths, embedded separators) are
rejected or stripped. No write may escape the upload directory.
*Acceptance:* a part with `filename="../../evil"` is stored as a safe basename
inside the target (or rejected `400`); no file appears outside the upload
directory. A regression test asserts containment via `realpath`.

**FR-UPLOAD-05 — Overwrite disabled by default.**
By default, an upload whose target name already exists does NOT overwrite;
servery either rejects with `409 Conflict` or auto-renames (deterministic suffix).
`--allow-overwrite` opts into replacing existing files.
*Acceptance:* uploading `a.txt` when `a.txt` exists, without `--allow-overwrite`,
does not modify the existing file (returns `409` or writes `a (1).txt`); with
`--allow-overwrite`, the existing file is replaced.

**FR-UPLOAD-06 — Upload target directory.**
Uploads land in the current served directory of the `POST` target by default; an
explicit `--upload-dir <path>` overrides the destination (must be inside root).
*Acceptance:* `POST /sub/` stores into `<root>/sub/` by default; with
`--upload-dir <root>/incoming`, uploads land in `incoming/` regardless of POST
path.

**FR-UPLOAD-07 — Directory creation is OUT of scope for v1.**
servery does not create new directories on upload (`mkdir`) in v1. Uploads to a
non-existent target directory fail rather than implicitly creating paths.
*Acceptance:* `POST` targeting a non-existent subdirectory returns `404`/`409`
and creates no directories. (Recorded decision; see §6 DEC-MKDIR.)

**FR-UPLOAD-08 — Upload UI and programmatic form.**
The directory listing exposes an upload control (a `multipart/form-data` form)
when `--upload` is active, and the same endpoint accepts programmatic
multipart/urlencoded POSTs.
*Acceptance:* with `--upload`, the listing page contains a file-input form; a
`curl -F file=@x` to the directory succeeds.

### 1.8 Archive Download (folder → zip / tar.gz)

**FR-ARCHIVE-01 — Download a folder as an archive.**
A directory may be downloaded as a single archive via a query trigger
(e.g. `?download=zip`, `?download=tar.gz`, `?download=tar`). Formats: `zip`
(`zipfile`), `tar` and `tar.gz` (`tarfile`).
*Acceptance:* `GET /sub/?download=tar.gz` returns `200` with
`Content-Type: application/gzip` (or `application/x-tar`/`application/zip`
respectively), `Content-Disposition: attachment; filename="sub.tar.gz"`, and a
body that extracts to the directory contents.

**FR-ARCHIVE-02 — Streaming, memory-bounded archives.**
Archives stream to the socket rather than being fully buffered in memory.
`tar`/`tar.gz` use `tarfile.open(fileobj=wfile, mode="w|gz")` (true streaming).
`zip` streams chunked writes (sets no `Content-Length`; uses chunked or
connection-close) to avoid buffering large trees.
*Acceptance:* archiving a directory whose total size exceeds available buffer
memory completes with peak process memory well below the tree size (streaming
test); `tar.gz` responses carry no `Content-Length` and a `Transfer-Encoding:
chunked` or a `Connection: close`.

**FR-ARCHIVE-03 — Archive respects root, symlink, and hidden policy.**
Archived contents honor the same containment and symlink rules as listing/serving
(no escaping root; symlinks not followed out of root unless `--follow-symlinks`).
Hidden files follow the `--show-hidden` policy.
*Acceptance:* a directory containing an out-of-root symlink produces an archive
that does not include the external target by default; with `--show-hidden`,
dotfiles are included.

**FR-ARCHIVE-04 — Archive availability is gated by listing being enabled.**
Archive download links appear only when directory listing is enabled. With
`--no-listing`, the `?download=` trigger is not offered (and MAY be refused).
*Acceptance:* with `--no-listing`, no archive link is rendered.

### 1.9 CORS / SPA / Cache / Clean URLs / Custom headers

**FR-CORS-01 — CORS toggle.**
`--cors` adds `Access-Control-Allow-Origin: *` to responses and answers `OPTIONS`
preflight with `204` + appropriate `Access-Control-Allow-*` headers.
*Acceptance:* with `--cors`, `GET /x` includes `Access-Control-Allow-Origin: *`;
an `OPTIONS /x` preflight returns `204` with `Access-Control-Allow-Methods`. Without
`--cors`, no `Access-Control-*` headers are present.

**FR-SPA-01 — SPA fallback (internal rewrite, no redirect).**
`--spa` (alias `--single`) serves `index.html` (or `--index`) for any request that
does not resolve to an existing file, via an internal rewrite (no `30x`).
*Acceptance:* with `--spa`, `GET /some/client/route` (no such file) returns `200`
with the `index.html` body and the **original** URL preserved (no `Location`
header / redirect). A real existing asset is still served as itself.

**FR-SPA-02 — Honor a magic `404.html`.**
Independent of `--spa`, if a `404.html` exists at root it is served (with `404`
status) for not-found paths.
*Acceptance:* with `404.html` present and `--spa` off, a not-found `GET` returns
`404` with the `404.html` body.

**FR-CACHE-01 — Cache-Control toggle.**
Cache-Control is configurable: `-c <seconds>` / `--cache <seconds>` sets
`Cache-Control: max-age=<n>`; `-c -1` (or `--no-cache`) disables caching
(`Cache-Control: no-cache, no-store`). Default: a conservative `no-cache` posture
suitable for a dev tool unless a max-age is set.
*Acceptance:* with `-c 3600`, responses carry `Cache-Control: max-age=3600`; with
`--no-cache` (or `-c -1`), responses carry `Cache-Control: no-cache` (and no
positive max-age).

**FR-CACHE-02 — ETag (OPTIONAL).**
servery MAY emit a weak `ETag` derived from size+mtime (`hashlib`) and honor
`If-None-Match` → `304`, complementing the inherited `Last-Modified`/`304`.
*Acceptance (if implemented):* a `GET` returns an `ETag`; a follow-up `GET` with
matching `If-None-Match` returns `304`.

**FR-CLEAN-01 — Clean/pretty URLs (OPTIONAL, opt-in).**
`--pretty-urls`: if a request path has no extension and `<path>.html` exists, it is
served; the `.html` form 301-redirects to the clean form.
*Acceptance (if implemented):* with `--pretty-urls` and `about.html` present,
`GET /about` serves it; `GET /about.html` returns `301` to `/about`.

**FR-HDR-01 — Custom response headers.**
`-H/--header "Name: Value"` (repeatable) injects arbitrary response headers,
reusing the base `extra_response_headers` hook. This is the escape hatch for HSTS,
extra CORS, etc.
*Acceptance:* `--header "X-Test: 1"` causes every response to include `X-Test: 1`;
the flag may be given multiple times and all are emitted.

### 1.10 Logging & Startup

**FR-LOG-01 — Request logging.**
Each request is logged (method, path, status, size where known) to stderr, reusing
the base `log_message`, with TTY-aware colorization preserved.
*Acceptance:* a `GET /x` emits a one-line log containing the method, path, and
status code; output is colorized only when stderr is a TTY.

**FR-LOG-02 — Informative startup banner.**
On start, servery prints: the bound address(es) and port, the served root, the URL
scheme (http/https), and the enabled features (auth on/off, upload on/off, TLS
on/off), plus the loud no-TLS-auth warning when applicable (FR-AUTH-04).
*Acceptance:* startup output contains the serving URL (e.g. `http://127.0.0.1:8000`),
the root path, and explicit on/off indicators for auth/upload/TLS.

**FR-LOG-03 — Bind-scope warning.**
Binding to a non-loopback address (notably `0.0.0.0` / `::`) emits a clear
"exposed on the network" warning at startup.
*Acceptance:* `--bind 0.0.0.0` emits a stderr warning mentioning network exposure;
the default localhost bind emits no such warning.

**FR-LOG-04 — Quiet / verbosity controls (OPTIONAL).**
`-q/--quiet` suppresses per-request logs (keeping the startup banner and warnings);
verbosity is otherwise at a sane default.
*Acceptance (if implemented):* with `-q`, per-request lines are suppressed while
the startup banner and warnings still appear.

---

## 2. CLI Surface

### 2.1 Entry points & equivalence

servery is invocable three ways, all behaviorally identical (Principle §4):

- `python -m servery [OPTIONS] [DIRECTORY]`
- `servery [OPTIONS] [DIRECTORY]` (console script declared in `pyproject.toml`)
- `import servery` (the CLI is a thin `argparse` wrapper over the public API;
  anything the CLI does is reachable from Python).

The positional `DIRECTORY` (default: current working directory) is the served
root. A bare `servery` serves the CWD on `127.0.0.1` with the rich listing and no
auth/upload/TLS.

### 2.2 Flag table

| Long | Short | Arg | Default | Description |
|------|-------|-----|---------|-------------|
| `--port` | `-p` | `PORT` | `8000` | TCP port to listen on. |
| `--bind` | `-b` | `ADDR` | `127.0.0.1` | Bind address. Non-loopback (e.g. `0.0.0.0`) triggers an exposure warning. |
| `--index` | | `NAME` | `index.html,index.htm` | Index document name(s) to serve for directories. |
| `--no-listing` | | flag | off | Disable directory listings (dir without index → 403/404). |
| `--show-hidden` | | flag | off | Include dotfiles in listings/archives. |
| `--sort` | | `name\|size\|date` | `name` | Default sort column for listings. |
| `--order` | | `asc\|desc` | `asc` | Default sort order for listings. |
| `--ignore-client-sort` | | flag | off | Ignore client `?C=&O=`; always use server default sort. |
| `--follow-symlinks` | | flag | off | Follow symlinks that resolve outside root (re-enables escape). |
| `--auth` | | `USER:PASS` | none | Enable Basic Auth (single shared credential). Pre-hashed: `USER:sha256:<hex>` / `USER:sha512:<hex>`. |
| `--tls-cert` | | `PATH` | none | TLS certificate (PEM). Enables HTTPS with `--tls-key`. |
| `--tls-key` | | `PATH` | none | TLS private key (PEM). |
| `--tls-password-file` | | `PATH` | none | File containing the private-key passphrase. |
| `--tls-client-ca` | | `PATH` | none | CA bundle for mutual TLS (require client certs). *(nice-to-have)* |
| `--tls-help` | | flag | — | Print an `openssl` self-signed cert one-liner and exit. |
| `--upload` | | flag | off | Enable file upload (POST). |
| `--upload-dir` | | `PATH` | POST target dir | Destination for uploads (must be within root). |
| `--max-upload-size` | | `BYTES` | `104857600` (100 MiB) | Maximum accepted upload size. |
| `--allow-overwrite` | | flag | off | Allow uploads to overwrite existing files. |
| `--cors` | | flag | off | Send `Access-Control-Allow-Origin: *` and answer preflight. |
| `--spa` / `--single` | | flag | off | SPA fallback: serve index for unknown paths (internal rewrite). |
| `--pretty-urls` | | flag | off | Serve `path.html` for extension-less `path`; 301 `.html`→clean. *(optional)* |
| `--cache` | `-c` | `SECONDS` | `no-cache` | `Cache-Control: max-age=SECONDS`. `-c -1` / `--no-cache` disables caching. |
| `--no-cache` | | flag | (default posture) | Send `Cache-Control: no-cache, no-store`. |
| `--content-type` | | `MIME` | `application/octet-stream` | Default content type for unknown extensions. |
| `--header` | `-H` | `"Name: Value"` | none | Add a custom response header (repeatable). |
| `--quiet` | `-q` | flag | off | Suppress per-request logs. *(optional)* |
| `--version` | | flag | — | Print version and exit. |
| `--help` | `-h` | flag | — | Print help and exit. |

Notes:
- `--cache`/`-c` and `--no-cache` are two spellings of one concern; `-c -1`
  equals `--no-cache` (http-server convention, borrowed).
- `--spa` and `--single` are aliases (servery vs `serve`/miniserve naming).
- mTLS (`--tls-client-ca`) and `--pretty-urls`/`--quiet`/ETag are explicitly
  marked nice-to-have/optional; they MUST default off and never change baseline
  behavior.

### 2.3 Configuration precedence

**Decision (DEC-CONFIG):** **CLI flags are the single source of truth for v1.**
There is no environment-variable or config-file layer in v1.

Rationale: keeping one configuration surface upholds Principle §6 (small, stable
surface) and avoids the "which setting won?" ambiguity. Environment-variable
config (à la `MINISERVE_*`) is a deliberate non-goal for v1 and may be revisited
later. If/when env config is added, the precedence will be **CLI > environment >
built-in default**, and that ordering is reserved now so it cannot be chosen
inconsistently later.

---

## 3. Non-Functional Requirements

**NFR-DEP-01 — Zero third-party dependencies (HARD).**
servery's runtime imports only the Python standard library. `pip install servery`
installs servery and nothing else. This outranks every other requirement.
*Acceptance:* `pyproject.toml` declares no `dependencies`; a clean install in an
empty venv followed by `python -c "import servery"` succeeds with no other
packages present; a CI check greps the source for imports and fails on any
non-stdlib top-level module.

**NFR-PY-01 — Python 3.13+ only.**
`requires-python = ">=3.13"`. The codebase uses the post-`cgi` world natively (one
hand-rolled multipart parser, no legacy `cgi` branch). The floor is raised only
deliberately, in a minor release, with a changelog note.
*Acceptance:* the package metadata declares `>=3.13`; CI runs the full test suite
on 3.13 and each newer supported CPython; no `import cgi` exists anywhere.

**NFR-SEC-01 — Safe defaults.**
Out of the box: bind `127.0.0.1`; no auth; no upload; no TLS; listings on;
dotfiles hidden; symlinks not followed out of root; path traversal blocked.
Every risky capability is explicit opt-in.
*Acceptance:* a bare `servery` is reachable only on loopback, refuses writes,
blocks traversal, and does not follow out-of-root symlinks — each covered by a
test.

**NFR-SEC-02 — Constant-time secrets, no secret leakage.**
Credential/digest comparisons use `hmac.compare_digest`; any generated tokens use
`secrets`; TLS key passphrases are read from a file, never accepted as a CLI arg
(avoids process-list exposure).
*Acceptance:* grep confirms no `==` credential comparison and no passphrase CLI
arg; a test confirms compare path uses `hmac.compare_digest`.

**NFR-SEC-03 — Honest posture (not production-hardened).**
servery does not claim DoS resistance, rate limiting, WAF behavior, CSRF
protection, or multi-tenant isolation. Docs state "safe defaults for trusted
networks; front it with a reverse proxy for exposure."
*Acceptance:* README/`--help` carry the not-for-hostile-internet statement; no
requirement here implies otherwise.

**NFR-PERF-01 — Concurrency via threading.**
servery serves via `ThreadingHTTPServer` / `ThreadingHTTPSServer`
(`socketserver.ThreadingMixIn`, `daemon_threads=True`), so a slow/large download
or upload does not block other clients.
*Acceptance:* two concurrent requests (one a long streaming download) are both
served without the second waiting for the first to finish (concurrency test).

**NFR-PERF-02 — Streaming for large payloads (memory-bounded).**
Large file downloads, range responses, archive generation, and uploads all stream
in bounded chunks; none buffers an entire payload in memory.
*Acceptance:* serving, archiving, and uploading a payload larger than a set memory
budget each complete with peak RSS well below the payload size (covered by
FR-RANGE-02, FR-UPLOAD-02, FR-ARCHIVE-02 tests).

**NFR-PORT-01 — Cross-platform (Linux / macOS / Windows).**
servery runs on Linux, macOS, and Windows. Path handling is
separator-agnostic; symlink semantics differences (Windows symlink privileges,
case-insensitive filesystems) are documented, and containment checks use
`os.path.realpath` consistently.
*Acceptance:* the test suite passes on all three OSes in CI; a documented caveats
section covers Windows symlink/`realpath` and case-folding behavior.

**NFR-PORT-02 — Dual-stack binding.**
When binding a wildcard address, servery clears `IPV6_V6ONLY` so both IPv4 and
IPv6 clients connect (mirroring the base `DualStackServerMixin`), where the OS
supports it.
*Acceptance:* a wildcard bind accepts both an IPv4 and an IPv6 client on a
dual-stack host.

**NFR-PKG-01 — Pure `pyproject.toml`, console script, module entry.**
Packaging is a single `pyproject.toml` with no dependencies, a `servery` console
entry point, and a working `python -m servery` (`__main__.py`). No build-time
third-party tooling is required beyond a standard PEP 517 backend.
*Acceptance:* `pip install .` in a clean venv yields a `servery` command and a
runnable `python -m servery`; metadata lists zero runtime deps.

**NFR-QA-01 — stdlib `unittest` only (no test-dependency).**
Tests use the standard-library `unittest` framework (and `unittest.mock`,
`http.client`, `tempfile`, `ssl`). No `pytest` or other third-party test
dependency. The "zero third-party" promise extends to the test/dev surface for
the test runner itself.
*Acceptance:* `python -m unittest` runs the full suite in a venv with only servery
installed; the project has no `pytest` in any dependency group required to run
tests.

**NFR-QA-02 — Readable, hackable source.**
Code favors clarity over cleverness (Principle §5): obvious stdlib calls, small
modules, no metaprogramming for its own sake — readable in an afternoon.
*Acceptance:* reviewer sign-off; the security-sensitive paths (path translation,
multipart parsing, auth) are each isolated, commented, and unit-tested.

**NFR-API-01 — Stable, small public API.**
A small public API (a configuration object/params, a handler class, a
`serve()`/`run()` entry) lets embedders construct and run servery from Python.
The CLI maps argv onto exactly these params. The API and flag set are kept small
and stable; new flags are treated as a cost (Principle §6).
*Acceptance:* a documented example constructs the server in Python without going
through argv and serves a directory; the public names are enumerated and covered
by tests.

---

## 4. Traceability

Each major feature maps to the principle/gap it satisfies and the prior-art
reference it borrows from.

| Feature | Satisfies (Vision/Principle) | Borrowed from (Reference) |
|---------|------------------------------|---------------------------|
| Rich sortable listing | Vision §2 headline gap ("no sizes/dates/sorting"); Principle §7 worked example | miniserve listing; Apache `mod_autoindex` `?C=&O=` scheme (REFERENCES §3.6) |
| Search/filter | Vision §2 gap | miniserve filter box (REFERENCES §3.4) |
| Range / `206` | Vision §1 ("no range support"); Principle §0 "stdlib lacks it; we add it" | The Range myth + recipe (REFERENCES §0.2, §5) |
| Basic auth (single, hashed, constant-time) | Principle §1 (constant-time), §0.7 scope (single credential) | uploadserver auth split + miniserve SHA-256/512 format; `hmac.compare_digest` (REFERENCES §3.1, §3.4, §5) |
| TLS (user cert/key, mTLS option) | Principle §0 (`ssl` only); Vision §1 | `http.server.HTTPSServer` `SSLContext` recipe; uploadserver mTLS (REFERENCES §1, §3.1) |
| TLS self-signed = openssl doc | Principle §0 (no dep, scope down) | "not zero-dep feasible" finding (REFERENCES §5/§6) |
| Streamed bounded upload | Principle §1 (opt-in, bounded, traversal-checked); §0 (no `cgi`) | `email.parser` route; Droopy/uploadserver make_file → temp → atomic rename (REFERENCES §0.1, §3.1, §3.3, §5) |
| Archive download (zip/tar.gz) | Vision §2 parity gap | woof directory→archive; miniserve `-z/-r/-g`; `tarfile w\|gz` streaming (REFERENCES §3.3, §3.4, §5) |
| CORS toggle | Vision §2 | `serve`/`http-server` `--cors` (REFERENCES §3.5) |
| SPA fallback / `404.html` | Vision §2 | `serve --single` rewrite + `http-server` magic `404.html` (REFERENCES §3.5) |
| Cache-Control `-c<n>`/`-c-1` | Vision §2 | `http-server -c<seconds>` / `-c-1` (REFERENCES §3.5) |
| Localhost-default bind + warnings | Principle §1 (safe by default) | http.server footgun avoidance (REFERENCES §1) |
| Path-traversal / symlink containment | Principle §1 | base `translate_path` (gh-87389); miniserve `--no-symlinks` realpath (REFERENCES §1, §3.4) |
| Threading + streaming | Principle §0 (build on base), §5 | `ThreadingHTTPServer` (REFERENCES §1) |
| Zero-dep / 3.13 / unittest-only | Principle §0, §3 | post-`cgi` reality (REFERENCES §0.1) |

---

## 5. Out of Scope (v1)

Recorded so they are not re-proposed:

- **Markdown README rendering** — no stdlib parser; escaped plaintext at most.
- **QR code** — no stdlib encoder.
- **WebDAV** — laborious protocol effort; deferred (zero-dep feasible later via
  `xml.etree`, but not v1).
- **Gzip response compression** — feasible (`gzip` + `Accept-Encoding`) but
  deferred from v1 as a smallness call; may be added later behind a flag.
- **Multi-user accounts / roles / sessions / database** — auth is one shared
  credential.
- **User-defined routes / endpoints / middleware / app object** — framework lane;
  permanently out.
- **Auto-generated TLS certs** — not zero-dep feasible; documented `openssl`
  command instead.
- **Directory creation / delete / chmod on upload** — write surface limited to
  bounded file upload in v1.
- **Environment-variable / config-file configuration** — CLI is the only config
  surface in v1 (DEC-CONFIG).

---

## 6. Decision Register (resolved open questions)

Smallest-safe-default rationale applied throughout.

**DEC-PY — Minimum Python = 3.13.** *(seeded, recorded)*
Post-`cgi`-removal; one clean hand-rolled multipart parser, no legacy branch.
A 3.11/3.12 backport is declined unless a concrete user need emerges.

**DEC-RANGE — servery implements Range itself.** *(seeded, recorded)*
stdlib does not provide it (verified). Single-range `bytes=a-b`, suffix, and
open-ended ranges are mandatory; `206`+`Content-Range`+`Accept-Ranges`; `416`
for unsatisfiable; malformed falls back to `200`. Multi-range optional.

**DEC-SYMLINK — Do not follow symlinks out of root by default.**
Default deny-escape (containment via `realpath`). `--follow-symlinks` opts in.
*Rationale:* a symlink must never become a traversal bypass; the smallest safe
default is to refuse out-of-root targets and make following an explicit choice.

**DEC-UPLOAD-OPTIN — Upload is opt-in, streamed, bounded.** *(seeded, recorded)*
Off unless `--upload`; parsed without `cgi`; streamed to a temp file then atomic
`os.rename`; never buffered whole in memory.

**DEC-UPLOAD-OVERWRITE — Overwrite disabled by default.**
Existing-name uploads do not overwrite by default (`409` or deterministic rename);
`--allow-overwrite` opts in. *Rationale:* a write tool must not silently destroy
data; non-destructive is the smallest safe default.

**DEC-UPLOAD-CAP — Default max upload size = 100 MiB.**
`--max-upload-size` (bytes) caps uploads; default `104857600`; exceeding → `413`.
*Rationale:* large enough for ad-hoc artifact transfer, small enough to bound a
single request's disk/time impact by default; users with bigger needs raise it
explicitly.

**DEC-MKDIR — Directory creation on upload is OUT for v1.**
No `mkdir`/delete/chmod write surface. *Rationale:* each additional write
operation is new security surface; v1 limits writes to bounded file upload into
existing directories (Principle §6 smallness).

**DEC-SEARCH — Search/filter is IN for v1.**
Client-light inline filter plus server-side `?q=`. *Rationale:* it is a headline
listing nicety, zero-dep (inline text + `os.scandir`), and core to the "listing
you don't have to apologize for" promise.

**DEC-ARCHIVE — Archive download is IN for v1.**
zip (`zipfile`) and tar/tar.gz (`tarfile`, streaming `w|gz`). *Rationale:*
zero-dep, file-server-lane, real parity gap vs miniserve/woof; streaming keeps it
memory-safe.

**DEC-AUTH — Single shared Basic credential; multi-user OUT.** *(seeded, recorded)*
`--auth user:pass`, with pre-hashed `user:sha256:<hex>`/`sha512` form;
`hmac.compare_digest`; loud warning if auth without TLS.
*Rationale:* matches Vision/Principle scope (an access gate, not an identity
system); the smallest credential model that meets the "gate it behind a password"
use case.

**DEC-TLS — User-provided cert/key ONLY; self-signed is a documented `openssl`
command, not a feature.** *(seeded, recorded)*
Pure stdlib cannot generate a self-signed cert, so servery prints
`openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"`
via `--tls-help` (and when cert/key are requested but absent) rather than
generating one. Optional mTLS via `--tls-client-ca` is a nice-to-have, off by
default.

**DEC-CONFIG — CLI is the only configuration surface in v1.**
No env/config-file layer. Reserved future precedence (if added):
**CLI > environment > default.** *Rationale:* one config surface (Principle §6);
no "which setting won?" ambiguity.

**DEC-CACHE — Default cache posture is `no-cache`.**
A dev tool should not encourage stale caching by default; `-c <seconds>` opts into
a positive max-age, `-c -1`/`--no-cache` is the explicit off sentinel
(http-server convention).

**DEC-HIDDEN — Dotfiles hidden by default (cosmetic, not access control).**
`--show-hidden` reveals them; hiding does not block a direct `GET` of a known
dotfile, and this is documented so it is not mistaken for protection.

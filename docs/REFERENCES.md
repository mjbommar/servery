# servery — References / Prior-Art

Research for **servery**: a zero-dependency, pure-Python **stdlib-only** HTTP file server —
"a batteries-included `python -m http.server`" in the spirit of miniserve / `npx serve`,
but with **ZERO third-party dependencies**.

> **Hard constraint, front of mind:** pure Python stdlib only. Every borrowed idea below is
> annotated with the stdlib path to implement it (`ssl`, `hmac`, `base64`, `email.parser`,
> `zipfile`, `tarfile`, `socketserver`, `http.server`, `urllib.parse`, `html`, `mimetypes`, …),
> or flagged as **not zero-dep feasible** (drop/defer/optional-extra).

Compiled mid-2026. Source URLs are cited inline. The CPython facts were verified directly
against the local checkout at `/home/mjbommar/src/cpython/Lib/http/server.py` (current `main`),
not just docs.

---

## 0. TL;DR — the three findings that change the design

1. **Multipart upload parsing post-Python 3.13 (THE big one).** `cgi` / `cgi.FieldStorage`
   was **removed in Python 3.13** (PEP 594, "dead batteries"). There is **no new stdlib
   multipart helper** (none added through 3.14). The **official zero-dep replacement is the
   `email` package**: feed the request body + `Content-Type` (with boundary) to
   `email.parser.BytesParser` / `email.message_from_bytes`, get an `EmailMessage`, walk
   `msg.iter_parts()`, read each part's `Content-Disposition` name/filename and
   `get_payload(decode=True)`.
   - Caveat: `email`/`BytesParser` is **not streaming** — it buffers the whole body in memory.
     For large uploads that loses the "stream to disk" property the old `FieldStorage.make_file()`
     hook gave us. Streaming multipart is the one place a third-party lib (`multipart`) is
     docs-blessed; a zero-dep server must either accept the memory cost or hand-roll a boundary
     splitter off `rfile`.
   - Prior-art reality check: `uploadserver` keeps `FieldStorage` alive by **vendoring a verbatim
     copy of 3.12's `cgi.py`**; `tiny-http-server`, `Droopy`, and `woof` all still call bare `cgi`
     and are therefore **broken on 3.13+** as written.
   - Source: <https://docs.python.org/3.13/whatsnew/3.13.html>, <https://peps.python.org/pep-0594/>,
     <https://docs.python.org/3/library/email.parser.html>

2. **Range requests are NOT free — the "since 3.7" claim is a myth.** Verified in the local
   `main` source: `SimpleHTTPRequestHandler.send_head()` unconditionally returns `200`, sets
   `Content-Length` to the **full** file size, and copies the whole file via
   `shutil.copyfileobj`. There is **no `Accept-Ranges`, no `206 Partial Content`, no
   `Content-Range`, and no parsing of the `Range:` header** anywhere in `Lib/http/server.py`.
   The belief is confusion with the third-party `rangehttpserver` package. Tracking issue
   [python/cpython#86809](https://github.com/python/cpython/issues/86809) is **open**; PRs
   [#24228](https://github.com/python/cpython/pull/24228) and
   [#118949](https://github.com/python/cpython/pull/118949) are **unmerged** (the latter went
   stale 2026-04, eyeing 3.15). **servery must implement Range itself** if it wants resumable
   downloads / media seeking. It's straightforward: parse `Range: bytes=start-end`, emit `206`
   with `Content-Range` + `Accept-Ranges: bytes`, `seek()`/bounded read.

3. **A LOT is already free from the stdlib base classes.** `http.server` gives us
   `ThreadingHTTPServer`, `HTTPSServer`/`ThreadingHTTPSServer` (modern `SSLContext` +
   ALPN, added recently), directory listing (`list_directory` via `os.scandir`), safe path
   translation (`translate_path` with `//`-open-redirect protection, gh-87389), MIME guessing
   (`guess_type`), `If-Modified-Since`/`304` handling, index-file serving, dual-stack bind, a
   `-H/--header` custom-header hook, and `--tls-cert/--tls-key/--tls-password-file` already wired
   in `_main`. **servery should subclass `SimpleHTTPRequestHandler`/`ThreadingHTTPServer` and
   extend, not reinvent.** See §1.

---

## 1. CPython `http.server` — what the base class already gives us

Repo: <https://github.com/python/cpython> · local: `/home/mjbommar/src/cpython/Lib/http/server.py`

This is the foundation servery should build on. Read it. Concretely, **already implemented**:

| Capability | Where (in `Lib/http/server.py`) | Notes |
|---|---|---|
| Threaded server | `ThreadingHTTPServer` (`socketserver.ThreadingMixIn`, `daemon_threads=True`) | Free concurrency. |
| HTTPS server | `HTTPSServer` / `ThreadingHTTPSServer` | Modern `ssl.create_default_context(Purpose.CLIENT_AUTH)` + `load_cert_chain` + ALPN (`http/1.1`). **Recently added to stdlib** — this is the correct TLS recipe to mirror, not `ssl.wrap_socket`. |
| Directory listing | `list_directory()` | Uses `os.scandir`, sorts case-insensitively, escapes via `html.escape`, `urllib.parse.quote` links, light/dark `color-scheme` CSS. **Plain `<ul>`, no sort/search/size/mtime** — this is exactly what servery upgrades. |
| Safe path mapping | `translate_path()` | Strips query/fragment, `posixpath.normpath`, drops `..`/drive components, **`//` open-redirect protection (gh-87389)**. Reuse; do not weaken. |
| Index files | `send_head()` + `index_pages = ("index.html","index.htm")` | Auto-serves index; 301-redirects dir without trailing slash (Apache-like). |
| Conditional GET | `send_head()` | `If-Modified-Since` → `304 Not Modified`. (No `ETag`/`If-None-Match` generation though.) |
| MIME types | `guess_type()` → `mimetypes.guess_file_type` | Has an `extensions_map` override for `.gz/.Z/.bz2/.xz`. |
| File send | `copyfile()` → `shutil.copyfileobj` | Whole-file only (see Range myth above). |
| Custom headers | `extra_response_headers` + `-H/--header` CLI | Repeatable header injection — the hook for CORS, Cache-Control, HSTS. |
| Dual-stack bind | `_main` `DualStackServerMixin` | Clears `IPV6_V6ONLY`. |
| Colorized logs | `log_message` / `_colorize` | TTY-aware request log coloring. |
| Custom default content-type | `--content-type` | For unknown extensions. |

**NOT provided (servery must add):** Range/`206`, sortable/searchable listing, file sizes & mtime
in the listing, basic auth, upload (`do_POST`), archive (zip/tar.gz) download of folders, CORS
toggle, SPA fallback, clean URLs, themes, QR, gzip response compression, WebDAV.

**Borrow:** subclass `SimpleHTTPRequestHandler`; override `list_directory`, add `do_POST`, add a
`send_head` that honors `Range`. Reuse `translate_path` verbatim (security-reviewed).
**Avoid:** don't fork the whole module; don't reimplement path traversal protection.

---

## 2. Comparison matrix (tools × features)

Legend: ● yes · ◐ partial/limited · ○ no · — n/a.
"Zero-dep" = could servery do this stdlib-only (not whether the tool itself is Python).

| Feature | **servery** target | http.server (stdlib) | uploadserver | tiny-http-server | Droopy | woof | miniserve (Rust) | serve (Node) | http-server (Node) |
|---|---|---|---|---|---|---|---|---|---|
| Rich listing (size/mtime) | ● | ○ | ○ | ○ | — | — | ● | ● | ◐ |
| Sortable columns | ● | ○ | ○ | ○ | — | — | ● | ○ | ○ |
| Search/filter listing | ● | ○ | ○ | ○ | — | — | ● | ○ | ○ |
| Basic auth | ● | ○ | ● | ● | ● | ○ | ● | ○ | ○ |
| Hashed passwords | ● | ○ | ○ | ○ | ○ | — | ● (SHA-256/512) | — | — |
| Upload | ● | ○ | ● | ● | ● | ◐ (`-U`) | ● | ○ | ○ |
| HTTPS / TLS | ● | ● | ● | ◐ (deprecated API) | ● | ○ | ● | ○ | ○ |
| mTLS (client cert) | ◐ defer | ○ | ● | ○ | ○ | ○ | ○ | ○ | ○ |
| CORS toggle | ● | ◐ (via `-H`) | ○ | ○ | ○ | ○ | ◐ (via `--header`) | ● | ● |
| SPA fallback | ● | ○ | ○ | ○ | ○ | ○ | ● (`--spa`) | ● (`--single`) | ◐ (`404.html`) |
| Clean URLs | ● | ○ | ○ | ○ | ○ | ○ | ● (`--pretty-urls`) | ● (default) | ○ |
| Cache-Control flag | ● | ◐ (via `-H`) | ○ | ○ | ○ | ○ | ● | ● (`-c<n>`/`-c-1`) | ● |
| Range / resumable | ● (must build) | ○ **(myth!)** | ○ | ○ | ○ | ○ | ● | ◐ | ◐ |
| Folder zip/tar.gz download | ● | ○ | ○ | ○ | ○ | ● (tar/zip) | ● (zip/tar/t.gz) | ○ | ○ |
| Gzip response compression | ● | ○ | ○ | ○ | ○ | ○ | ● | ◐ | ◐ |
| QR code | ✗ not zero-dep | ○ | ○ | ○ | ○ | ○ | ● | ○ | ○ |
| Themes (light/dark) | ● | ◐ (color-scheme) | ○ | ○ | ○ | ○ | ● | ○ | ○ |
| README markdown render | ✗/◐ subset | ○ | ○ | ○ | ○ | ○ | ● | ○ | ○ |
| WebDAV | ◐ defer | ○ | ○ | ○ | ○ | ○ | ● (read-only) | ○ | ○ |
| **Language** | Python | Python | Python | Python | Python | Python | Rust | Node | Node |
| **Zero third-party deps** | **● (goal)** | ● | ● (vendors `cgi`) | ● | ● | ● | n/a | ○ | ○ |

---

## 3. Per-reference notes

### 3.1 uploadserver (Densaugeo) — closest maintained pure-stdlib sibling
Repo: <https://github.com/Densaugeo/uploadserver> · PyPI: <https://pypi.org/project/uploadserver/>
(v6.0.1, 2026-02-23, "tested 3.9–3.14"). Zero-dep (no `install_requires`).
- **Does well:** active, broad Python-version coverage, real TLS **and mTLS**, streamed uploads.
- **Multipart approach (the key datum):** version-gated import — `import cgi` on <3.13, else
  `import uploadserver.cgi` (a **~1,100-line verbatim copy of 3.12's `cgi.py`** with the
  deprecation warning commented out). `PersistentFieldStorage(cgi.FieldStorage)` overrides
  `make_file()` to stream to `tempfile.NamedTemporaryFile(delete=False)` then `os.rename()`
  (atomic, memory-safe). Source:
  [`__init__.py`](https://raw.githubusercontent.com/Densaugeo/uploadserver/master/uploadserver/__init__.py),
  [vendored `cgi.py`](https://raw.githubusercontent.com/Densaugeo/uploadserver/master/uploadserver/cgi.py).
- **TLS/mTLS:** `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` + `load_cert_chain`; for
  mTLS adds `load_verify_locations(cafile=...)` + `verify_mode = ssl.CERT_REQUIRED`. **Correct
  modern recipe — borrow this.**
- **Auth:** `base64.b64decode` of the `Authorization` header; `--basic-auth` (all) vs
  `--basic-auth-upload` (uploads only). **But compares creds with plain `==` (not constant-time).**
- **BORROW:** the upload-only-vs-all auth split; streamed `make_file()` → temp → atomic rename;
  the `SSLContext`/`CERT_REQUIRED` mTLS recipe.
- **AVOID:** vendoring 1,100 lines of frozen stdlib (you inherit any future `cgi` CVE); plain `==`
  credential compare (use `hmac.compare_digest`); strategic reliance on the dead `cgi` API. For
  servery, prefer the `email.parser` route (§5) over vendoring.

### 3.2 tiny-http-server (johann-petrak) — the "what not to do" contrast case
Repo: <https://github.com/johann-petrak/python-tiny-http-server> ·
PyPI: <https://pypi.org/project/tiny-http-server/>. ~420-line single module. Zero-dep but
**stale** (~12 months no release) and **broken on 3.13+** (bare `cgi.FieldStorage`, no mitigation).
- **Does well:** clean multi-source auth — merge repeatable `--auth USER:PASS` with an
  `--authfile` (lines `user:pass`) into a dict. `--enable-override` to control overwrite.
- **Note vs the brief:** passwords are **plaintext, NOT hashed** (the "hashed entries" premise
  does not hold here — that's miniserve, §3.4). Check is `users.get(user) == passwd` (not
  constant-time).
- **AVOID:** `ssl.wrap_socket(...)` (deprecated — use `SSLContext.wrap_socket`); `field.file.read()`
  loading the **whole upload into memory** (README itself warns it can "bog down the machine");
  single-process; plaintext creds on the CLI.
- **BORROW:** the dict-merge auth-source pattern and honest README warnings.

### 3.3 Droopy (stackp) & woof (Simon Budig) — single-file minimalism
- **Droopy:** <https://github.com/stackp/Droopy> — single ~1,114-line `droopy` script; an
  *upload* server (inverse of a file server): HTML5 multi-file upload, HTTPS, Basic auth, custom
  message/picture. Subclasses `cgi.FieldStorage` with a `make_file()` override that writes
  **directly into the destination dir** (no temp copy). **Pre-cgi-removal → won't run on 3.13+
  unmodified** (still carries Py2/Py3 shims). BORROW: the drop-in single-file executable ethos and
  the direct-to-target streaming idea (must be re-created without `FieldStorage`).
- **woof** ("Web Offer One File"): <https://github.com/simon-budig/woof/> (**archived 2026-01**),
  ~573-line `woof` script. Serves *one* file N times then exits; can serve a directory as an
  on-the-fly **tar/zip**; `-U` flips to upload mode; `-s` serves its own source. Uses
  `cgi.parse_header` + `cgi.FieldStorage` → **also broken on 3.13+**. Maintained fork:
  <https://github.com/timotree/woof3>. BORROW: transparent directory→archive streaming; the
  serve-then-self-terminate lifecycle as an optional `--once` mode.

### 3.4 miniserve (svenstaro, Rust) — the FEATURE north star
Repo: <https://github.com/svenstaro/miniserve> · README + [v0.22.0 notes](https://github.com/svenstaro/miniserve/releases/tag/v0.22.0) ·
[DeepWiki usage](https://deepwiki.com/svenstaro/miniserve/1.1-installation-and-usage).
Full feature list with zero-dep feasibility tags:

- **Listing:** sortable cols (name/size/date, `-S`/`-O`), search/filter box, human/exact sizes
  (`--size-display`), mtime, breadcrumbs, dirs-first (`-D`), recursive dir size
  (`--directory-size`), hidden toggle (`-H`). **[all zero-dep feasible]** — `os.scandir`/`os.stat`,
  server-side sort, embedded JS filter.
- **Archive download:** `.zip` (`-z`), `.tar` (`-r`), `.tar.gz` (`-g`). **[zero-dep feasible]** —
  `zipfile` / `tarfile` (`w:gz`); stream to socket to avoid miniserve's in-memory caveat.
- **QR code** (`-q`). **[NOT zero-dep]** — no stdlib QR encoder; would need to hand-roll
  Reed-Solomon/masking. **Defer or optional-extra.**
- **Themes** (light `-c` / dark `-d`, selector UI). **[zero-dep feasible]** — static CSS strings +
  `prefers-color-scheme` + a JS toggle.
- **WebDAV** (`--enable-webdav`, read-only `PROPFIND`). **[zero-dep feasible but laborious]** —
  emit `207` multistatus XML via `xml.etree.ElementTree`; dispatch `do_PROPFIND`/`do_OPTIONS`.
  **Defer.**
- **TLS** (`--tls-cert`/`--tls-key`) + HSTS. **[zero-dep feasible]** — `ssl.SSLContext`.
  **No mTLS in miniserve** (correcting the brief — it's server-side TLS only).
- **SPA** (`--spa`), `--index`, `--pretty-urls` (`/about`→`about.html`). **[zero-dep feasible]** —
  fallback in the handler.
- **README render** (`--readme`, GitHub-style markdown; plaintext for `README`/`README.txt`).
  **[NOT zero-dep for full markdown]** — stdlib has no markdown parser. Plaintext `<pre>` path is
  trivial; a reduced in-house subset renderer is possible but not GFM-fidelity.
- **Auth:** Basic, **hashed SHA-256/SHA-512**, multiple users (`-a`/`--auth-file`), optional blank
  pw. **[zero-dep feasible]** — `base64` + `hashlib` + `hmac.compare_digest`. This is exact parity
  (miniserve uses raw SHA-256/512, not bcrypt/argon2).
- **Upload/file ops:** upload (`-u`), concurrency limit, mkdir (`-U`), delete (`-R`), duplicate
  handling error/overwrite/rename (`-o`), media-type restriction (`-m`/`-M`), chmod (`--chmod`),
  temp dir, pastebin. **[zero-dep feasible]** — `os.makedirs`/`os.remove`/`os.chmod`; **the only
  fiddly bit is multipart parsing (no stdlib parser since `cgi`, see §5).**
- **Other:** `--route-prefix`, `--random-route` (`secrets.token_hex(3)`), `-P/--no-symlinks`
  (`os.path.realpath` containment), `--header` (repeatable; also the CORS route),
  `-C/--compress-response` (`gzip` + check `Accept-Encoding`), `-I/--disable-indexing`,
  healthcheck, env-var config (`MINISERVE_*`), `--workers`. **[all zero-dep feasible]** —
  `argparse`, `socketserver.ThreadingMixIn`.
- **Range requests** — miniserve has them; servery must build them (§0.2). **[zero-dep feasible]**.
- **BORROW:** the entire feature taxonomy + flag names as servery's roadmap; SHA-256/512 hashed
  auth file format; archive-on-the-fly. **AVOID/DEFER:** QR and full markdown (not zero-dep);
  WebDAV (laborious — defer).

### 3.5 npm `serve` (Vercel) & `http-server` (http-party) — UX conventions only
`serve`: <https://github.com/vercel/serve> + engine <https://github.com/vercel/serve-handler>.
`http-server`: <https://github.com/http-party/http-server>. (Node — borrow **conventions**, not code.)
- **SPA fallback — two idioms:** `serve --single`/`-s` = rewrite all not-found → `index.html`
  (internal rewrite, no redirect; sugar for serve.json `{"rewrites":[{"source":"**","destination":"/index.html"}]}`).
  `http-server` has **no `--single`**; instead a **magic `404.html`** is served on miss. **BORROW
  both:** a `--spa`/`--single` rewrite flag *and* honor a `404.html` if present.
- **Clean URLs:** serve's `cleanUrls` is **default-on** — serve `/about` from `/about.html`, and
  301-redirect the `.html` form to the clean form. BORROW as an opt-in flag.
- **Cache-Control:** `http-server -c<seconds>` (default 3600), **`-c-1` disables**. **BORROW this
  exact `-c<n>` / `-c-1` convention** — clean single-flag max-age with an explicit "off" sentinel.
  (serve does ETag/`304` and leaves caching opt-in via the `headers` config key.)
- **CORS:** both use **`--cors`** → `Access-Control-Allow-Origin: *`; http-server optionally takes
  comma-separated values added to `Access-Control-Allow-Headers`. **BORROW the `--cors` flag name.**
- **Listing/index as independent toggles:** http-server `-d` (listing) vs `-i` (autoindex). BORROW
  the separation.
- **Key semantic to preserve:** *rewrite = internal, no redirect*; *redirect = 30x to client*.

### 3.6 Apache `mod_autoindex` — the battle-tested sort URL scheme to mirror
Docs: <https://httpd.apache.org/docs/2.4/mod/mod_autoindex.html> ("Autoindex Request Query Arguments").
With `FancyIndexing` on, column headers are self-referencing links carrying:
- **`C=` (column):** `N`=Name · `M`=last-Modified(then name) · `S`=Size(then name) · `D`=Description.
- **`O=` (order):** `A`=Ascending · `D`=Descending.
- **`F=` (format):** `0`=plain · `1`=fancy · `2`=HTMLTable. **`V=`** version sort (0/1). **`P=`**
  wildcard pattern filter.
- **Combined / semicolon form:** `?C=M&O=D` — Apache also accepts `;`: `?C=M;O=D` (both treated as
  arg separators). Emit `&`, accept both.
- **Toggle behavior:** clicking a header re-sorts by that column; clicking the **same** header
  again toggles `O=A`↔`O=D`. New column resets to `O=A`. Each header `<a href>` encodes the *next*
  state.
- Server-side knobs to mirror: `IndexOrderDefault Ascending|Descending Name|Date|Size|Description`
  (initial order); `IndexOptions IgnoreClient` (server flag to ignore client sort params — a good
  hardening toggle).
- **BORROW:** the `?C=<N|M|S|D>&O=<A|D>` scheme verbatim so anyone who's used Apache gets servery's
  sortable listing for free, **JS-free**. Optionally honor `P=` for glob filtering.

---

## 4. "Clone these" list

```bash
# 1. The foundation — read send_head / list_directory / translate_path / HTTPSServer.
#    (Already present locally at /home/mjbommar/src/cpython)
git clone https://github.com/python/cpython.git

# 2. Closest maintained pure-stdlib sibling: streamed uploads, real TLS + mTLS,
#    and the canonical "vendored cgi" workaround for 3.13. Study its make_file() + SSLContext.
git clone https://github.com/Densaugeo/uploadserver.git

# 3. Contrast case (what to avoid): plaintext creds, ssl.wrap_socket, in-memory uploads,
#    bare cgi. Useful as a small, readable single-module skeleton + multi-source auth pattern.
git clone https://github.com/johann-petrak/python-tiny-http-server.git

# 4. Single-file upload-server minimalism; the make_file()-to-destination idea.
git clone https://github.com/stackp/Droopy.git

# 5. Directory->tar/zip streaming + serve-N-times lifecycle (use the maintained Py3 fork).
git clone https://github.com/timotree/woof3.git

# 6. THE feature north star (Rust). Don't borrow code — mine the README/CLI for the full
#    feature + flag taxonomy and the auth-file (SHA-256/512) format.
git clone https://github.com/svenstaro/miniserve.git

# 7. UX conventions: SPA --single / serve.json rewrites, cleanUrls, 404.html semantics.
git clone https://github.com/vercel/serve-handler.git

# 8. UX conventions: the -c<seconds> / -c-1 cache flag, --cors, -d/-i listing-vs-index split,
#    magic 404.html SPA fallback.
git clone https://github.com/http-party/http-server.git
```

(`mod_autoindex` is documentation, not a repo to clone — see §3.6 link.)

---

## 5. stdlib capability map (feature → module → confidence/caveats)

| Feature | stdlib path | Confidence | Caveats |
|---|---|---|---|
| **HTTPS / TLS** | `ssl.SSLContext` via `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` + `ctx.load_cert_chain(certfile, keyfile)` + `ctx.wrap_socket(sock, server_side=True)`. Mirror `http.server.HTTPSServer`. | **High** | Use `SSLContext`, **never** the deprecated `ssl.wrap_socket`. Set ALPN `["http/1.1"]`. |
| **mTLS (client cert)** | Same context + `ctx.load_verify_locations(cafile=ca)` + `ctx.verify_mode = ssl.CERT_REQUIRED`. | **High** | Optional/defer; uploadserver shows the exact recipe. |
| **Basic auth** | Parse `Authorization: Basic <b64>` → `base64.b64decode`; split `user:pass`; compare with **`hmac.compare_digest`** (constant-time). On miss: `401` + `WWW-Authenticate: Basic realm="..."`. | **High** | Don't use `==` (timing leak). Decode is `latin-1`/`utf-8`. |
| **Hashed password file** | `hashlib.sha256`/`sha512` (miniserve parity) or `hashlib.pbkdf2_hmac`/`hashlib.scrypt` (salted, stronger); compare digests with `hmac.compare_digest`. | **High** | Pick a file format up front (e.g. `user:sha256:hexdigest`). Salted KDF preferred over raw SHA. |
| **Multipart upload parse (post-3.13)** | **`email.parser.BytesParser`** / `email.message_from_bytes` on `Content-Type` (+boundary) + body; iterate `msg.iter_parts()`; per part read `Content-Disposition` name/filename + `get_payload(decode=True)`. Read body via `Content-Length` off `rfile`. | **Medium-High** | **`cgi` removed in 3.13.** `email` is the official zero-dep route but **buffers whole body in memory** (no streaming). For large/streaming uploads, either hand-roll boundary splitting off `rfile` to disk, or accept the memory cost. `urllib.parse.parse_qsl` covers non-multipart form bodies. Write to a temp file + atomic `os.rename` (Droopy/uploadserver pattern). |
| **Zip folder download** | `zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED)`; `os.walk` + `zf.write`. | **High** | In-memory `BytesIO` is simplest but RAM-bound. For large trees, write to a `tempfile` then stream, or use a streaming-zip approach (chunked writes to `wfile`). Can't set `Content-Length` if streaming → use chunked/connection-close. |
| **tar / tar.gz folder download** | `tarfile.open(fileobj=wfile, mode="w\|gz")` (note the **`\|`** streaming mode → write straight to the socket, no seek). | **High** | `w\|gz` is genuinely streaming (unlike zip). Great for "download folder". |
| **Range / resumable** | Parse `Range: bytes=a-b`; `f.seek(a)`; send `206` + `Content-Range: bytes a-b/total` + `Accept-Ranges: bytes` + bounded `Content-Length`. | **High** | **Not in stdlib — build it.** Handle suffix ranges (`bytes=-N`), open-ended (`bytes=a-`), and `416` for unsatisfiable. Single-range is enough; multipart/byteranges optional. |
| **Sortable listing** | Pure server-side: read `?C=&O=` (Apache scheme, §3.6), sort `os.scandir` entries by name/size/mtime, render header links encoding the next sort state. | **High** | 100% JS-free. `os.DirEntry.stat()` for size/mtime; guard `OSError` per entry (broken symlinks). |
| **Search/filter listing** | Small **embedded** JS that filters `<li>`/rows client-side (no server round-trip), OR a server-side `?q=` substring filter. | **High** | "Embedded JS" is still zero *third-party*-dep (it's inline text we ship). Server-side `?q=` keeps it pure-server if desired. |
| **CORS** | `send_header("Access-Control-Allow-Origin", origin)` (+ methods/headers); handle `OPTIONS` preflight (`do_OPTIONS` → `204`). | **High** | Mirror `--cors` → `*`. Reuse the base `extra_response_headers` hook. |
| **SPA fallback** | In the handler: if path not found and not a real file, internally serve `index.html` (rewrite, no redirect). Also honor a `404.html` if present. | **High** | Don't redirect; rewrite. Guard against rewriting asset/`/api` paths. |
| **Clean URLs** | If `path` has no extension and `path + ".html"` exists, serve it; 301 the `.html` form → clean form. | **High** | serve has this default-on; make it an opt-in flag. |
| **Cache-Control** | `send_header("Cache-Control", f"max-age={n}")` (or `no-cache` for `-c-1`). Optionally add `ETag` (`hashlib` of size+mtime) + `If-None-Match`→`304`. | **High** | Base class already does `Last-Modified`/`If-Modified-Since`→`304`; add `ETag` for completeness. |
| **Gzip response compression** | Check `Accept-Encoding: gzip`; `gzip.compress(body)` or wrap with `gzip.GzipFile`; set `Content-Encoding: gzip`. | **High** | Only worth it for text; skip already-compressed types. Drop `Content-Length` if streaming. |
| **MIME types** | `mimetypes.guess_file_type(path)` (added 3.13; preferred for paths). `guess_type(url)` is soft-deprecated for file paths. | **High** | Base class still calls `guess_type` internally — switch servery to `guess_file_type`. Keep an `extensions_map` override. |
| **Threading / concurrency** | `socketserver.ThreadingMixIn` (use `ThreadingHTTPServer`); `daemon_threads=True`. | **High** | Already free. Consider a worker/connection cap for upload concurrency. |
| **Symlink safety** | `os.path.realpath(target)` must start with `realpath(root)`; `os.path.islink` to hide. Reuse `translate_path`. | **High** | Add a `--no-symlinks` containment check; don't weaken the base `translate_path`. |
| **Random/route-prefix obscurity** | `secrets.token_hex(3)` for `--random-route`; string-prefix all routes for `--route-prefix`. | **High** | Trivial. |
| **WebDAV (read-only)** | `do_PROPFIND`/`do_OPTIONS`; build `207` multistatus XML with `xml.etree.ElementTree`. | **Medium** | Laborious protocol work; **defer**. No parser dep needed. |
| **QR code** | — | **Not feasible zero-dep** | No stdlib QR encoder. Hand-rolling (Reed-Solomon + masking) is large/error-prone. **Drop, or gate behind an optional extra (`segno`).** |
| **Markdown README render** | — (plaintext `<pre>` via `html.escape` is fine) | **Not feasible (full) zero-dep** | No stdlib markdown parser. Options: (a) plaintext fallback only **[zero-dep]**; (b) ship a tiny in-house subset renderer (headings/bold/code/links/lists) — not GFM-fidelity; (c) optional extra (`markdown`/`mistune`). |

---

## 6. "Reach vs grasp" — miniserve features in pure Python stdlib

**Comfortably within grasp (zero-dep):** rich/sortable/searchable listing, file sizes & mtime,
breadcrumbs, dirs-first, hidden toggle, themes (light/dark + selector), archive download
(zip via `zipfile`, tar/tar.gz via `tarfile w|gz` — the latter truly streaming), TLS **and** mTLS
(`ssl.SSLContext`), basic auth with SHA-256/512 hashed multi-user files (`hashlib`+`hmac`), SPA
fallback, clean/pretty URLs, route-prefix, random route, no-symlinks containment, custom headers,
CORS, gzip response compression, disable-indexing, healthcheck, `--workers`, env-var config.
**Plus Range/`206`** (not in stdlib but easy to add). All of miniserve's *core* file-server value
is achievable stdlib-only.

**Fiddly but doable (zero-dep, with caveats):**
- **Upload / multipart** — no stdlib parser since `cgi` was removed; use `email.parser.BytesParser`
  (in-memory) or hand-roll boundary splitting for streaming-to-disk. The single trickiest "easy"
  feature.
- **WebDAV** — `xml.etree` can build the `207` XML, but it's real protocol effort. **Defer.**

**Out of grasp (NOT zero-dep — drop, defer, or make optional extras):**
- **QR code** — no stdlib encoder. (Optional extra: `segno`/`qrcode`.)
- **Full GitHub-fidelity markdown README rendering** — no stdlib markdown parser. Zero-dep options
  are only a plaintext `<pre>` fallback or a reduced in-house subset renderer; full GFM needs a
  third-party lib. (Optional extra: `markdown`/`mistune`.)

**Net:** servery can match ~90% of miniserve's feature surface stdlib-only. The honest gaps are
**QR** and **full markdown** — both should be omitted from the zero-dep core (or shipped as opt-in
extras), not faked. The one feature people *assume* is free but isn't — **Range requests** — is
cheap to build and worth doing early (media seeking + resumable downloads).

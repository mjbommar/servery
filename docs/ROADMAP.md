# servery — Roadmap

> From an empty repo to a polished, PyPI-published v1.0 — sequenced by
> value-to-effort and dependency order, foundation before features.

This roadmap is governed by `PRINCIPLES.md` (Principle 0 above all: **zero
third-party dependencies, pure Python stdlib, forever**) and scoped by
`VISION.md`. Every milestone is independently shippable. Each lists a **goal**,
**in scope**, **out of scope (for now)**, **exit criteria**, and the **primary
stdlib modules** involved.

Verified design facts that shape the order (see `REFERENCES.md`):

- **Range is not free.** Stdlib `send_head()` always returns `200` with the full
  `Content-Length`. servery must implement `Range`/`206` itself.
- **`cgi` is gone (3.13).** Multipart upload parsing is hand-rolled — the single
  trickiest "easy" feature, so it gets its own late milestone.
- **A lot *is* free.** Threading, HTTPS server class, `translate_path` traversal
  protection, `If-Modified-Since`/`304`, index files, MIME guessing, dual-stack
  bind. We subclass and extend; we do not reinvent.

---

## Definition of Done (every milestone must satisfy ALL of these)

A milestone is not "shipped" until:

- [ ] **Zero deps verified.** `pyproject.toml` declares no runtime
      dependencies; a CI check fails the build if `dependencies` is non-empty or
      anything but `servery` is importable from a clean install.
- [ ] **Tests pass via stdlib `unittest`.** New behavior covered by
      `unittest`-based tests (using `http.client` / `urllib.request` against a
      live `ThreadingHTTPServer` on an ephemeral port). No third-party test
      runner. Coverage measured with stdlib facilities where used.
- [ ] **Runs on every supported CPython** (3.13 and each newer non-EOL release),
      exercised in CI on Linux, macOS, and Windows.
- [ ] **Safe defaults intact.** Still binds `127.0.0.1` by default;
      path-traversal and symlink-escape protections still hold (regression test
      present); no new feature weakens them or turns risky behavior on without an
      explicit flag.
- [ ] **Docs updated.** README usage, CLI `--help`, and any new flag documented
      in the same change. Public API docstrings current.
- [ ] **CLI ↔ library parity.** Anything the new flag does is reachable from the
      Python API; the CLI only parses argv into config (Principle 4).
- [ ] **Friendly startup/failure.** Startup banner reflects new state (e.g. auth
      on, TLS on, the no-TLS-auth warning); errors say what to do.
- [ ] **Changelog entry** written; version bumped per the policy below.

---

## Milestone overview

| Ver | Goal (one line) | Headline exit criterion |
|-----|-----------------|-------------------------|
| **v0.1** | Walking skeleton: installable, runnable, *rich* listing, safe bind | `python -m servery` shows size/mtime/dir-first listing on localhost |
| **v0.2** | Sortable + searchable listing (Apache `?C=&O=`) | Clicking a column re-sorts JS-free; `?q=` filters |
| **v0.3** | Range/resumable downloads + correct caching headers | `curl -r` returns `206` + `Content-Range`; `ETag`/`304` work |
| **v0.4** | TLS (user cert/key) | `--tls cert.pem key.pem` serves HTTPS via `SSLContext` |
| **v0.5** | Basic Auth (+ hashed, constant-time, no-TLS warning) | `--auth u:p` gates access; wrong creds → `401`; loud HTTP warning |
| **v0.6** | Upload (opt-in, streamed, bounded, overwrite-off) | `--upload` accepts multipart to disk, bounded, no traversal |
| **v0.7** | Archive download (zip / tar.gz) | `?archive=tar.gz` streams a folder; zip option present |
| **v0.8** | CORS + SPA fallback + cache flags | `--cors`, `--spa`, `-c<n>`/`-c-1` behave per conventions |
| **v0.9** | Hardening & polish (logging, error pages, cross-platform, coverage) | Themed error pages; Windows-clean; high coverage |
| **v1.0** | Stability + packaging + PyPI release | `pip install servery` from PyPI; API/CLI frozen for 1.x |

---

## v0.1 — Walking skeleton (MVP)

**Goal.** The smallest end-to-end thing that is *already better* than
`python -m http.server`: installable, three entry points, and a directory
listing with sizes, dates, human-readable units, and directories first — bound
safely to localhost.

**In scope:**

- Project scaffolding: pure `pyproject.toml` (no build-time third-party deps;
  `requires-python = ">=3.13"`), `src/servery/` layout, `__main__.py` so
  `python -m servery` works, a `servery` console-script entry point, and an
  importable public API (a `serve()`/config object + handler/server classes).
- Subclass `SimpleHTTPRequestHandler` + `ThreadingHTTPServer`; reuse
  `translate_path` verbatim (security-reviewed) and the dual-stack bind mixin.
- **Rich listing:** override `list_directory` to render name, **size**
  (human-readable, e.g. `2.4 KB`), **mtime** (local, ISO-ish), **dirs-first**,
  case-insensitive sort. Inline server-rendered HTML/CSS via `html.escape` +
  `string.Template`; `urllib.parse.quote` links; light/dark via
  `prefers-color-scheme`. Guard per-entry `OSError` (broken symlinks).
- Switch MIME lookup to `mimetypes.guess_file_type` (path-preferred, 3.13+).
- **Safe-default bind 127.0.0.1**; `--host 0.0.0.0` is the explicit opt-in.
- Basic CLI (stdlib `argparse`): `[directory]`, `--host`, `--port`, `--bind`
  alias, `--version`. Friendly startup banner (bound address).

**Out of scope (for now):** sorting/search UI, Range, TLS, auth, upload,
archives, CORS/SPA, themes selector, hidden-file toggle.

**Exit criteria:**

- `pip install -e .` then `python -m servery`, `servery`, and
  `import servery; servery.serve(...)` all serve the cwd with the rich listing.
- Listing shows size + mtime + dirs-first and renders correctly for an empty
  dir, a dir with subdirs, and a dir with a broken symlink (no crash).
- Default bind is `127.0.0.1`; a test asserts it does **not** bind `0.0.0.0`
  without the flag.
- Path-traversal regression test passes (`..`, encoded, absolute tricks all 404
  or stay within root).
- All Definition-of-Done boxes checked.

**Primary stdlib modules:** `http.server`, `socketserver`, `os` (`scandir`,
`stat`), `html`, `string`, `urllib.parse`, `mimetypes`, `argparse`, `datetime`.

---

## v0.2 — Sortable & searchable listing

**Goal.** Make the headline listing sortable and filterable the way people
already expect — mirroring Apache `mod_autoindex` so it is familiar and JS-free
for the core path.

**In scope:**

- Server-side sort via the **Apache `?C=&O=` scheme**: `C=N|M|S` (name / mtime /
  size), `O=A|D` (asc/desc). Column headers are self-referencing links that
  encode the *next* sort state (same column toggles `A`↔`D`; new column resets to
  `A`). Emit `&`, accept both `&` and `;` separators.
- Server-side `?q=` substring filter (pure-server, no round-trip needed) **plus**
  an optional small **inline** JS filter box (still zero *third-party* dep — it
  is text we ship) for instant client-side narrowing.
- Breadcrumbs; `--hidden` toggle to show dotfiles (default hide).
- `IgnoreClient`-style server flag to ignore client sort params (hardening
  toggle).

**Out of scope (for now):** recursive directory-size totals, version sort
(`V=`), `P=` glob filter (may revisit), theme *selector* UI.

**Exit criteria:**

- Clicking each column header re-sorts correctly and toggles order; verified
  with no JavaScript enabled (server-side).
- `?C=S&O=D` returns entries largest-first; `?q=foo` returns only matching
  entries; both compose.
- Sort is stable and dirs-first is preserved within each order.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `os.scandir`/`DirEntry.stat`, `urllib.parse`,
`html`, `string`.

---

## v0.3 — Range / resumable downloads + caching headers

**Goal.** Make large files and media behave: resumable downloads and seeking,
plus correct, controllable cache validation. Pure handler work with no new
config surface coupling — which is why it precedes TLS/Auth.

**In scope:**

- Override `send_head`/file-send to honor **`Range: bytes=a-b`**: emit
  `Accept-Ranges: bytes` always; for a valid range, `206 Partial Content` +
  `Content-Range: bytes a-b/total` + bounded `Content-Length`, `seek()` + bounded
  read. Handle suffix (`bytes=-N`) and open-ended (`bytes=a-`) forms; `416` for
  unsatisfiable. Single-range only (multipart byteranges out of scope).
- **Caching:** keep the base class's `Last-Modified` / `If-Modified-Since` →
  `304`; add an **`ETag`** derived from size+mtime (`hashlib`) and honor
  `If-None-Match` → `304`.

**Out of scope (for now):** multi-range responses, gzip response compression
(lands in v0.8 with the other header flags), the `Cache-Control` *flag* (v0.8).

**Exit criteria:**

- `curl -r 0-99 <url>` returns `206`, correct `Content-Range`, exactly 100 bytes;
  suffix and open-ended ranges work; an out-of-bounds range returns `416`.
- A resumed/aborted download completes correctly across two range requests.
- Conditional GET with a stale `If-None-Match` returns `304`; fresh returns
  `200`.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `http.server`, `os`/`io` (`seek`, bounded read),
`hashlib`, `email.utils` (HTTP-date), `shutil`.

---

## v0.4 — TLS (user-provided cert/key)

**Goal.** Serve over HTTPS using the modern stdlib recipe — no self-signed
generation (the stdlib cannot mint certs), so cert/key are user-provided.

**In scope:**

- `--tls CERT KEY` (and `--tls-cert` / `--tls-key`, plus
  `--tls-password-file` for an encrypted key) wired through the server.
- Build context via `ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)` +
  `ctx.load_cert_chain(...)` + ALPN `["http/1.1"]`, mirroring
  `http.server.HTTPSServer` / `ThreadingHTTPSServer`. Never `ssl.wrap_socket`.
- Startup banner reflects `https://` and the bound address.

**Out of scope (for now):** mTLS / client-cert verification (deferred — see
backlog), stdlib self-signed cert generation (impossible; documented as such),
HSTS header (lands with header flags in v0.8).

**Exit criteria:**

- With a test cert/key fixture, `--tls` serves HTTPS; an HTTPS client retrieves a
  file and a listing successfully.
- A missing/invalid cert or key fails fast with a clear, actionable error.
- Plain-HTTP behavior is unchanged when `--tls` is absent.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `ssl`, `http.server` (HTTPS server class), `socket`.

---

## v0.5 — Basic Auth (single shared credential, hashed, constant-time)

**Goal.** Gate the server behind a single shared credential — safely: never plain
`==`, and loud when used without TLS.

**In scope:**

- `--auth USER:PASS` (single shared credential). Parse
  `Authorization: Basic <b64>` (`base64.b64decode`); compare **both** username
  and password with **`hmac.compare_digest`** (constant-time). On miss: `401` +
  `WWW-Authenticate: Basic realm="servery"`.
- **Hashed form:** accept a hashed credential (e.g. `user:sha256:<hexdigest>`,
  miniserve-parity) so the plaintext password need not sit on the command line;
  compare digests with `hmac.compare_digest`. Pick and document the format up
  front.
- **Loud no-TLS warning:** if auth is enabled without `--tls`, print a prominent
  startup warning that Basic Auth over HTTP sends credentials effectively in the
  clear. Never imply auth-over-HTTP is private.

**Out of scope (for now):** multiple users / auth files, roles/sessions/accounts,
salted KDF as the *only* option (raw SHA for parity is acceptable; a salted
`pbkdf2`/`scrypt` form may be added later), upload-only-vs-all auth split
(revisit when upload lands).

**Exit criteria:**

- No/invalid credentials → `401` with `WWW-Authenticate`; correct credentials →
  `200`. Hashed-credential form authenticates equivalently.
- A timing-independent comparison is used (code review confirms no `==` on
  secrets); a test asserts both username and password are checked.
- Enabling auth without TLS emits the loud warning (captured in a test).
- Definition-of-Done satisfied.

**Primary stdlib modules:** `base64`, `hmac`, `hashlib`, `secrets`,
`http.server`.

---

## v0.6 — Upload (opt-in, streamed, bounded, overwrite-off)

**Goal.** Let the other side send files back — safely. This is the trickiest
"easy" feature (no `cgi` since 3.13), so it lands after the security primitives
(auth, TLS) it should usually run behind.

**In scope:**

- `--upload` (off by default) enabling `do_POST` to an upload target directory.
- **Multipart parsing without `cgi`:** parse the `Content-Type` boundary and
  split the body into parts; per-part headers via `email.parser` /
  `email.message`; simple url-encoded bodies via `urllib.parse.parse_qsl`. Read
  the body off `rfile` bounded by `Content-Length`.
- **Streamed to disk, bounded:** write each part to a `tempfile` then atomic
  `os.rename` into place (Droopy/uploadserver pattern). Enforce a configurable
  **max upload size**; reject oversize with `413`.
- **Safety:** sanitize/refuse traversal in submitted filenames (no `..`, no
  absolute paths, basename only); **overwrite OFF by default** (`409`/rename on
  collision; `--overwrite` to opt in); never write outside the upload target.
- Minimal upload UI (an upload control in the listing when enabled).

**Out of scope (for now):** mkdir / delete / chmod / move file-ops, drag-drop
fanciness, resumable/chunked uploads, concurrency caps beyond the threading
default, full streaming multipart for pathological huge single parts (bounded +
temp-file is the contract; document the memory boundary honestly).

**Exit criteria:**

- A multipart POST writes the file to the target dir with correct bytes; a
  url-encoded form body is parsed too.
- Upload is **off** without `--upload` (POST → `405`/`403`); a traversal filename
  is rejected; an oversize upload returns `413`; a collision does not overwrite
  unless `--overwrite`.
- Uploaded data is streamed to a temp file and atomically renamed (verified no
  partial file is visible at the destination on failure).
- Definition-of-Done satisfied.

**Primary stdlib modules:** `email.parser`/`email.message`, `urllib.parse`,
`tempfile`, `os` (`rename`, `path`), `shutil`, `http.server`.

---

## v0.7 — Archive download (zip / tar.gz)

**Goal.** Download a whole folder as one file, streamed so memory stays bounded.

**In scope:**

- A download trigger on directories (e.g. `?archive=tar.gz` / `?archive=zip`,
  with a button in the listing).
- **tar.gz:** `tarfile.open(fileobj=wfile, mode="w|gz")` — genuinely streaming
  straight to the socket (no seek, no temp file). Preferred path.
- **zip:** `zipfile.ZipFile` with `ZIP_DEFLATED`; stream chunked to the socket
  (or temp-file-then-stream) to avoid an in-RAM full archive. Since streamed
  archives cannot set `Content-Length`, use chunked transfer / connection-close
  appropriately.
- Respect path-traversal/symlink rules while walking (`os.walk`); skip entries
  that escape the root.

**Out of scope (for now):** plain `.tar` (low value; revisit), per-file
selection, archive of arbitrary cross-directory selections, compression-level
tuning.

**Exit criteria:**

- `?archive=tar.gz` of a folder streams a valid gzip tar that extracts to the
  original tree; `?archive=zip` likewise extracts correctly.
- Memory stays bounded for a large tree (no full-archive buffering for tar.gz;
  documented bound for zip).
- Symlinks/paths that would escape the root are not included.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `tarfile`, `zipfile`, `os.walk`, `http.server`.

---

## v0.8 — CORS + SPA fallback + cache/header flags

**Goal.** The static-site/dev-server conveniences, as a cluster of small header-
and routing-level flags that share machinery.

**In scope:**

- **`--cors`** → `Access-Control-Allow-Origin: *` (+ methods/headers); handle
  `OPTIONS` preflight (`do_OPTIONS` → `204`).
- **`--spa` / `--single`** SPA fallback: not-found, non-file paths are
  *internally rewritten* (no redirect) to `index.html`; also honor a `404.html`
  if present. Guard against rewriting asset/API-looking paths.
- **`-c<n>` / `-c-1`** Cache-Control (http-server convention): `max-age=n`, with
  `-c-1` as the explicit "no cache" sentinel.
- Optional **clean URLs** (opt-in): serve `/about` from `/about.html`,
  301-redirect the `.html` form to the clean form.
- **gzip response compression** (opt-in): honor `Accept-Encoding: gzip` for text
  types, set `Content-Encoding: gzip`, skip already-compressed types.
- Custom headers via the base `extra_response_headers` / `-H` hook (HSTS, etc.).

**Out of scope (for now):** per-origin CORS allowlists, route-prefix / random
route (low priority; backlog), healthcheck endpoint (backlog).

**Exit criteria:**

- `--cors` sets the header and answers preflight `OPTIONS` with `204`.
- With `--spa`, a deep non-file path returns `index.html` body with a `200` (no
  redirect); a present `404.html` is served on miss.
- `-c3600` sets `Cache-Control: max-age=3600`; `-c-1` disables caching.
- gzip path returns `Content-Encoding: gzip` only for eligible types and decodes
  to the original bytes.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `http.server`, `gzip`, `urllib.parse`, `os.path`.

---

## v0.9 — Hardening & polish

**Goal.** Turn a feature-complete tool into a trustworthy one: good logs, good
error pages, cross-platform correctness, and real test depth.

**In scope:**

- **Logging:** TTY-aware request logging (reuse/extend `log_message` /
  `_colorize`); a `--quiet` and a `--verbose`/access-log toggle; clear,
  consistent startup banner summarizing host/port, TLS on/off, auth on/off (+
  no-TLS warning), upload on/off.
- **Error pages:** themed, escaped HTML for `401/403/404/413/416/500` matching
  the listing's look; never leak filesystem paths or stack traces to clients.
- **Cross-platform:** Windows path quirks (drive letters, separators, reserved
  names), case-insensitive filesystem behavior, correct mtime/size formatting;
  CI matrix proves Linux/macOS/Windows parity.
- **Security pass:** dedicated tests for path traversal, symlink escape
  (`os.path.realpath` containment + `--no-symlinks`), upload filename sanitation,
  constant-time auth; a written threat-model note in docs.
- **Test depth:** raise coverage across handlers; add fixtures for large files,
  broken symlinks, unusual filenames (unicode, spaces, `%`), and concurrency.
- **Docs:** complete README with the one-liners from the Vision, a CLI reference,
  an embedding/`import servery` example, and the honest "not for production"
  posture.

**Out of scope (for now):** new user-facing features; performance micro-tuning
beyond avoiding obvious O(n²)/full-buffer patterns.

**Exit criteria:**

- Error responses are themed, escaped, and leak nothing; verified per status
  code.
- Full CI matrix (all supported CPython × Linux/macOS/Windows) green.
- Security regression suite passes (traversal, symlink, upload sanitation,
  timing-safe auth).
- Docs cover every flag and the public API; the Vision one-liners all work
  verbatim.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `logging`/`http.server` logging, `html`, `os.path`,
`os` (`realpath`), `unittest`, `platform`.

---

## v1.0 — Stability & release

**Goal.** Freeze a small, stable surface and ship it to PyPI with the zero-dep
promise mechanically guaranteed.

**In scope:**

- **API/CLI freeze:** finalize public names, flag spellings, config object,
  exit codes, and the hashed-credential / upload-target formats. Document what is
  public (stable) vs internal.
- **Packaging:** complete `pyproject.toml` metadata (classifiers, license,
  `requires-python = ">=3.13"`, console-script), `LICENSE`, long-description from
  README, reproducible sdist + wheel built with a stdlib-friendly backend, **no
  runtime dependencies** asserted by a release gate.
- **Release engineering:** tagged release, CHANGELOG, CI job that builds and
  publishes to PyPI; a post-publish smoke test that `pip install servery` in a
  clean venv pulls **only** servery and runs.
- **Quality bars:** test coverage target met; all supported CPythons green;
  security suite green.

**Out of scope (for now):** anything in the north-star backlog.

**Exit criteria:**

- `pip install servery` from PyPI in a clean environment installs nothing but
  servery and runs all three entry points.
- A release gate fails if any runtime dependency is introduced.
- Version is `1.0.0`; public API/CLI documented as frozen for the 1.x line.
- Definition-of-Done satisfied.

**Primary stdlib modules:** `unittest`; packaging via standards-based
`pyproject.toml` (build backend is tooling, not a servery runtime dep).

---

## Release & versioning policy

servery follows **Semantic Versioning**.

- **Pre-1.0 (`0.y.z`):** each `0.y` minor is a shippable, usable increment, but
  the public API and CLI flags **may change between minors** as the design
  settles. `0.y.z` patches are bug-fix only. Breaking changes are called out in
  the changelog; we deprecate loudly when we can, but pre-1.0 does not promise
  stability.
- **1.0 promise:** the public Python API and the CLI flag surface are
  **stable for the 1.x line**. Backward-incompatible changes require a `2.0`.
  New features arrive in `1.y` minors (additive, opt-in, safe-default-preserving);
  fixes in `1.y.z` patches.
- **The one promise that never has a version:** *zero third-party runtime
  dependencies, forever.* It is not a feature with a version; it is the contract.
  A release gate enforces it on every build.
- **Python floor** moves only deliberately, in a minor release, with a changelog
  note — never silently (Principle 3). We test every supported, non-EOL CPython
  from the floor (3.13) up.

---

## North-star backlog (explicitly deferred / maybe-never)

Parked ideas, each with the reason. Nothing here blocks 1.0.

| Idea | Status | Why parked |
|------|--------|------------|
| **QR code for the share URL** | Maybe-never | **No stdlib QR encoder.** Hand-rolling Reed-Solomon/masking is large and error-prone; faking it betrays the zero-dep simplicity. Drop, or only ever as an optional extra. |
| **Full Markdown README rendering** | Maybe-never | **No stdlib Markdown parser.** Most we'll do is escaped plaintext `<pre>`. A reduced in-house subset renderer is possible but not GFM-fidelity and not worth the surface. |
| **WebDAV (read-only `PROPFIND`)** | Deferred | Zero-dep feasible (`xml.etree` for `207` multistatus) but laborious real protocol work; low payoff for the file-server lane. Revisit only on real demand. |
| **mTLS (client-cert verification)** | Deferred | Zero-dep feasible (`load_verify_locations` + `CERT_REQUIRED`); deferred as advanced/niche beyond the single-credential auth model. |
| **Themes selector UI** | Deferred | `prefers-color-scheme` covers the 90% case in v0.1; an explicit light/dark selector is polish, not core. |
| **Recursive directory-size totals** | Deferred | Useful but can be O(n) expensive on big trees; needs a bounded/cached design before it's worth it. |
| **mkdir / delete / chmod / move file-ops** | Deferred | Each widens the write surface and the security/threat model; upload alone covers the core "send me a file" need. Add only with strong opt-in gating. |
| **Route-prefix / random-route / healthcheck** | Deferred | Trivial individually but each is a flag with permanent cost (Principle 6); bundle later only if users ask. |
| **Multi-user auth / auth-file** | Deferred | The model is a *single shared credential* (Vision non-goal: no accounts/roles/sessions). A multi-entry file is the most we'd consider, and only on demand. |
| **`--once` / serve-N-times lifecycle** (woof-style) | Maybe | Cute and zero-dep, but niche; backlog until requested. |
| **User-defined routes / app endpoints / middleware** | Never | Framework lane. Hard non-goal (Vision §5, Principle 2). This is the line that keeps servery finishable. |

---

## Risk register

| Risk | Likelihood × Impact | Mitigation |
|------|---------------------|------------|
| **Hand-rolled multipart (no `cgi`) is subtly wrong** — boundary edge cases, CRLF handling, header parsing | Med × High | Parse part headers with `email.parser` (don't reinvent header parsing); bound everything by `Content-Length`; a focused fuzz/fixture suite of real browser multipart bodies (Chrome/Firefox/curl) before shipping v0.6; reject malformed boundaries strictly. |
| **Large-file / large-upload memory blowup** | Med × High | Downloads: Range + `seek`/bounded reads, never buffer whole files. Archives: stream `tar w|gz` to the socket; chunked zip. Uploads: stream to `tempfile` + atomic rename, enforce a max-size with `413`. Document the one honest memory bound (pathological single huge multipart part). |
| **Path-traversal / symlink-escape bypass** | Low × Critical | Reuse stdlib `translate_path` verbatim (security-reviewed, includes `//` open-redirect fix); add `os.path.realpath` containment + `--no-symlinks`; sanitize upload filenames to basename, reject `..`/absolute; dedicated security regression suite (v0.9) run every CI. |
| **Windows path quirks** — drive letters, separators, reserved names, case-insensitivity | Med × Med | Use `os.path`/`pathlib` consistently (no string slicing of paths); CI matrix includes Windows from v0.1; tests for reserved names and unusual filenames; never construct paths by concatenation. |
| **Timing/credential leak in auth** | Low × High | Always `hmac.compare_digest` on both username and password; never `==` on secrets; code-review gate + a test asserting both fields are checked; generate any nonces with `secrets`. |
| **Basic Auth over plain HTTP misunderstood as private** | Med × Med | Loud startup warning when auth is on without TLS; docs are explicit that base64 ≠ encryption; banner restates TLS state. |
| **Accidental dependency creep** | Low × Critical (breaks the promise) | Release gate that fails the build if `dependencies` is non-empty or anything but `servery` imports from a clean install; reviewer checklist item; Principle 0 is the tie-breaker on every design call. |
| **Streamed archive/gzip can't set `Content-Length`** → client confusion | Low × Med | Use chunked transfer-encoding / connection-close deliberately; test that clients (curl, browsers) receive complete archives; document the streaming contract. |
| **Concurrency races on upload** (same target file, atomic visibility) | Low × Med | Temp-file + atomic `os.rename`; overwrite-off by default with collision handling; tests under concurrent POSTs. |
| **Scope creep toward "a worse Flask"** | Med × High (to the project's identity) | Every feature runs the §7 scope rubric; the north-star backlog records the *no*s with reasons so they aren't re-litigated; "the default answer is no." |

# Architecture

> Companion to `VISION.md`, `PRINCIPLES.md`, and `REFERENCES.md`. This document
> describes *how* servery is built. The supreme constraint from `PRINCIPLES.md`
> §0 governs everything below: **zero third-party dependencies, pure Python
> standard library, forever.** Every class and function named here is stdlib.

Target: **CPython 3.13+** (`requires-python = ">=3.13"`). The floor matters
architecturally — `cgi`/`cgi.FieldStorage` was removed in 3.13, so multipart
upload parsing is hand-rolled (§6), not delegated.

---

## 1. Build-on-vs-rewrite decision

**Decision: subclass, do not rewrite or vendor.** servery extends
`http.server.SimpleHTTPRequestHandler` and serves via
`http.server.ThreadingHTTPServer` / `ThreadingHTTPSServer`. We do **not** fork
`Lib/http/server.py`, and we do **not** vendor a copy of it (the trap
`uploadserver` fell into by vendoring 3.12's `cgi.py` — inheriting any future
CVE in frozen stdlib).

### Why subclass

The base class already implements the un-fun, security-sensitive HTTP plumbing
correctly and keeps it patched by the CPython security team for free:

- **Request line / header parsing** — `BaseHTTPRequestHandler.parse_request`,
  including the `//` → `/` rewrite that closes the gh-87389 open-redirect.
- **Method dispatch** — `handle_one_request` resolves `do_<METHOD>` by name; we
  add behavior by *defining methods*, not by editing a dispatcher.
- **Response framing** — `send_response`, `send_header`, `end_headers`,
  `flush_headers`, status/date/server headers, `send_error` with XSS-escaped
  error bodies.
- **TLS** — `HTTPSServer` / `ThreadingHTTPSServer` build a modern
  `ssl.SSLContext` via `ssl.create_default_context(Purpose.CLIENT_AUTH)` +
  `load_cert_chain` + ALPN `["http/1.1"]`. This is the correct recipe; we will
  not hand-roll `ssl.wrap_socket` (deprecated).
- **Threading** — `socketserver.ThreadingMixIn` with `daemon_threads = True`.
- **Conditional GET** — `If-Modified-Since` → `304` lives in `send_head`.
- **Directory redirect** — `send_head` 301-redirects a dir lacking a trailing
  slash, Apache-style.

Reimplementing any of this would be strictly worse: more code to secure, less
battle-tested, and a violation of Principle 5 ("boring, readable, hackable" — the
base class *is* the boring path).

### What we reuse verbatim vs override vs add

| Base member | Disposition | Rationale |
|---|---|---|
| `parse_request`, `handle_one_request`, `handle` | **reuse** | HTTP plumbing; never touch. |
| `translate_path` | **reuse as the core, wrap with a containment check** | Already strips query/fragment, `posixpath.normpath`s, drops `..`/drive components. servery wraps it in `security.py` to add `realpath` containment + symlink policy (§5) — it does not weaken it. |
| `send_response`/`send_header`/`end_headers`/`send_error` | **reuse** | Response framing. |
| `guess_type` | **reuse, retargeted** | Keep the method; ensure it routes through `mimetypes.guess_file_type` (the 3.13 path-aware API) rather than the soft-deprecated `guess_type(url)`. |
| `send_head` | **override** | The single GET/HEAD choke-point. servery's version adds auth gate, path-safety, `Range`/`206`, SPA/clean-URL fallback, and cache/CORS headers, then delegates the actual file open back to base semantics where possible. |
| `list_directory` | **override** | The headline feature: rich sortable listing (size/mtime/sort/breadcrumbs) replaces the plain `<ul>`. |
| `copyfile` | **override** | Attempt kernel zero-copy via `self.connection.sendfile(source)` on the full-file `200` path — but **only when the connection is not an `ssl.SSLSocket`** and `source` exposes a real `fileno()`; otherwise fall back to a bounded `shutil.copyfileobj(..., length=64*1024)`. The `Range` `206` path keeps the bounded `seek`+chunked-write loop (NFR-PERF-03; `handler.py`, with the TLS/concurrency wrappers in `server.py`). |
| `protocol_version` | **override (class attr)** | Set to `"HTTP/1.1"` to enable persistent connections + framing guarantees (FR-CONN-01). |
| `timeout` | **override (class attr)** | Set a per-request socket timeout (default 30 s) so `StreamRequestHandler.setup` calls `settimeout` — Slowloris mitigation (NFR-PERF-04). |
| `log_message` / `log_request` | **override** | Route through `logging.getLogger("servery")` (with a library `NullHandler`) instead of writing straight to `sys.stderr`; track the real byte count for access logs (FR-LOG-05/06; `_log.py`). |
| `process_request` (server) | **override (conditional)** | When `config.max_workers` is set, submit `process_request_thread` to a bounded `concurrent.futures.ThreadPoolExecutor` instead of an unbounded thread (NFR-PERF-04; `server.py`). |
| `do_GET` / `do_HEAD` | **reuse** | They already just call `send_head`; all our logic lands in `send_head`. |
| `do_POST` | **add** | Upload (§6). Absent in the base class. |
| `do_OPTIONS` | **add (conditional)** | CORS preflight when `--cors` is set. |
| `extra_response_headers` + `_send_extra_response_headers` | **reuse as a hook** | The base class already injects repeatable response headers; we drive CORS / `Cache-Control` / custom `-H` through it. |
| `ThreadingHTTPServer` / `ThreadingHTTPSServer` | **subclass thinly** | Add the dual-stack `IPV6_V6ONLY` clear and `finish_request` kwarg injection (`config=...`), mirroring stdlib `_main`'s `DualStackServerMixin`. |

> **Note on `send_head`.** In the stdlib, `send_head` both *opens the file and
> sends 200 + headers*. Because we need to inject `206`/`Content-Range` and the
> auth/fallback decisions before the open, servery reimplements the body of
> `send_head` rather than calling `super().send_head()`. This is the one place we
> deliberately re-state base logic (the dir-redirect, index-file, and
> `If-Modified-Since` branches) — it is small, well-understood, and the seam where
> all our GET features must compose.

### Deliberate improvements over the stdlib base

Subclassing inherits the base's correct HTTP plumbing — and four of its 2026-era
*weaknesses*. servery fixes each one deliberately, zero-dep, in a named seam.
(Full RFC rationale in `STANDARDS.md`; implementation rationale in
`BEST-PRACTICES.md`.)

| Inherited weakness (stdlib base) | servery fix | Where |
|---|---|---|
| **HTTP/1.0 default, keep-alive off.** `protocol_version = "HTTP/1.0"` gates off persistent connections; a listing of N assets means N connections. | Set `protocol_version = "HTTP/1.1"` to flip keep-alive on via the base's existing logic; honor `Connection: close`; frame every streamed (`Content-Length`-less) body with chunked or `Connection: close` so a reused socket never hangs. (FR-CONN-01, NFR-STD-01) | `server.py`/`handler.py` (class attr); framing audit across `ranges`/`archive` |
| **No zero-copy.** `copyfile` is a userspace `shutil.copyfileobj` read/write loop; never calls `sendfile`. | Override `copyfile` to use `socket.sendfile()` (kernel `os.sendfile`, internal fallback) on the full-file `200` path, with a bounded `copyfileobj` fallback — and **skip sendfile for `ssl.SSLSocket`** (TLS must encrypt in userspace). (NFR-PERF-03) | `handler.py` (override), TLS guard in `server.py` |
| **No timeout, unbounded threads.** `socketserver` `timeout = None`; `ThreadingMixIn` spawns an uncapped thread per connection → Slowloris + thread/FD exhaustion exposure. | Set a per-request socket `timeout` (default 30 s) so stalled I/O raises `TimeoutError`; offer an optional `concurrent.futures.ThreadPoolExecutor` cap via `--max-workers` (default still unbounded). A mitigation, not a hardening promise (NFR-SEC-03). (NFR-PERF-04) | `handler.py` (`timeout` attr); `server.py` (`process_request`) |
| **Logs straight to `sys.stderr`.** `log_message` writes to stderr with no level/handler; an embedder cannot redirect or silence it; access logs always show `-` for size. | Route through `logging.getLogger("servery")` with a library `NullHandler` (library quiet, CLI loud); track the real byte count and status; swallow expected client disconnects without tracebacks; optional CLF/Combined access log. (FR-LOG-05/06/07) | `_log.py`; `handler.py` (`log_message`/`log_request`) |

These are additive overrides at named seams — never a fork of the base. Each is
gated or defaulted so a bare `servery` stays minimal and safe.

---

## 2. Package / module layout

`src/` layout (PEP 621, src-layout — see §8). Each module has one
responsibility; the dependency arrows point inward toward `config`.

```
servery/
├── pyproject.toml
├── README.md
├── docs/                     # VISION / PRINCIPLES / REFERENCES / TRANSPORTS / ARCHITECTURE
├── src/
│   └── servery/
│       ├── __init__.py       # public API: serve(), Config, ServeryHandler, make_server()
│       ├── __main__.py       # `python -m servery` → cli.main()
│       ├── py.typed          # PEP 561 marker (typed package)
│       ├── _version.py       # __version__ (single source of the version string)
│       ├── cli.py            # argparse → Config; main(); startup banner + warnings; --http2/--http3 wiring
│       ├── config.py         # frozen Config dataclass (the single source of truth)
│       ├── server.py         # ServeryHTTPServer / ServeryHTTPSServer, dual-stack, make_server(), TLS wrap
│       ├── handler.py        # ServeryHandler(SimpleHTTPRequestHandler): send_head, list_directory, do_POST, do_OPTIONS; h2/h2c dispatch
│       ├── security.py       # path containment + symlink policy; the one choke-point
│       ├── _log.py           # logging.getLogger("servery") + NullHandler; request/access logging
│       ├── ranges.py         # Range header parse → (start, end); 206/416 helpers; bounded emit
│       ├── auth.py           # Authenticator: Basic parse, hmac.compare_digest, hashed-credential format
│       ├── upload.py         # do_POST body: multipart streaming parser → temp → os.replace
│       ├── archive.py        # on-the-fly zip / tar.gz of a directory streamed to wfile
│       ├── listing.py        # directory → sorted entries → HTML (inline templates + ?C=&O= sort scheme)
│       ├── _oscrypto.py      # ctypes bindings to OS crypto (libssl/libcrypto / CNG) — opt-in transport use only
│       ├── http3.py          # optional HTTP/3 backend (aioquic, the servery[http3] extra); Http3UnavailableError
│       └── http2/            # pure-stdlib HTTP/2 transport tier (subpackage)
│           ├── __init__.py
│           ├── hpack.py      # HPACK (RFC 7541): static/dynamic table + Huffman encode/decode
│           ├── frames.py     # HTTP/2 binary framing (RFC 9113 §4/§6): HEADERS/DATA/SETTINGS/…
│           └── connection.py # H2Connection: stream state machine, flow control, DoS limits; dispatch into the handler
└── tests/                    # unittest only (§7)
```

Note: listing HTML/CSS is rendered inline from `listing.py` (no separate
`_templates.py` module — the `string.Template` strings live with the renderer).

### Why this split

- **`config.py` is the hub.** A frozen `Config` is the only thing the CLI
  produces and the only thing the server/handler consume. Every feature gate is
  a `Config` field, so feature logic never reads `argparse` namespaces or env —
  it reads `self.server.config`. This is also what makes the library equal to the
  CLI (`PRINCIPLES.md` §4): an embedder builds a `Config` and calls `serve()`.
- **`handler.py` stays an orchestrator, not an implementation.** It owns the
  overridden methods (`send_head`, `list_directory`, `do_POST`) but each one is a
  short sequence of calls into a single-purpose module. Listing rendering, range
  math, auth, upload parsing, and archiving each live *outside* the handler so
  the handler reads as a request lifecycle, not a 1,000-line god-class.
- **`security.py` is isolated on purpose.** Path safety is the highest-stakes
  code; keeping it in one small module with its own tests (§5, §7) means the
  traversal/symlink rules are reviewable in one place and can't drift across
  features.
- **`ranges.py` vs `archive.py` vs `listing.py`** are split by HTTP concern so
  the default download path (no range, no archive) pulls in almost nothing.
- **Header-emission logic — ETag + the conditional-request ladder, security
  headers, Cache-Control/CORS, and `Content-Disposition` — lives with the GET
  choke-point** (driven from `handler.send_head`, steps [5]/[6] below), not in a
  separate `httputil`/`conditional` module: the validator/precedence logic
  (FR-COND-01/02), the default security headers (FR-SEC-04/05), and the RFC
  6266/8187 filename builder (FR-DISP-01) are small, stateless helpers over the
  request headers + `os.stat` result. Keeping the closely-coupled "decide status +
  emit headers" logic together avoids fragmenting the ladder from the
  `ETag`/`Cache-Control` it depends on.
- **`_log.py` IS its own module**, because routing through `logging` + a library
  `NullHandler` + the request/access-log formatting is a distinct responsibility
  (FR-LOG-05/06) that the handler should call into, not own. The handler's
  `log_message`/`log_request` overrides are one-liners that delegate here.
- **Listing templates are data, not a module** — inline `string.Template` HTML/CSS
  rendered straight from `listing.py`, shipped in the wheel; no build step,
  satisfying "no asset pipeline" (`PRINCIPLES.md` §0).
- **The transport tiers are the one deliberate subpackage.** `http2/` (pure
  stdlib) groups the HPACK + framing + connection state machine that together
  implement the HTTP/2 tier; `http3.py` is the optional aioquic backend; both are
  imported **only** when their flag is set, and never on the default GET path. See
  §2.1 below and `docs/TRANSPORTS.md`.

We keep the core deliberately small and flat. The **only** subpackage is `http2/`
— a transport tier whose 2–4k LOC of framing/HPACK/state-machine genuinely earns a
package boundary (and stays cleanly behind a flag) — not a framework leaking in
(`VISION.md` §5). The opt-in `http3.py` and the `_oscrypto.py` ctypes shim are
likewise transport-only and off the default path.

### 2.1 HTTP/2 (http2/) and HTTP/3 (http3.py) — how the tiers slot in

The transport tiers (`docs/TRANSPORTS.md`) attach at the connection seam without
touching the file-serving core. The request-handling pipeline (`send_head` /
`do_POST` / listing / range / auth) is shared; a tier owns only *transport*
(framing, multiplexing, flow control), never file-serving policy.

- **HTTP/2 — `http2/` (pure stdlib, `--http2`).** `handler.handle` detects HTTP/2
  on a connection — via TLS ALPN negotiating `h2`, or the h2c prior-knowledge
  cleartext preface — and dispatches to `http2.connection.H2Connection` instead of
  the line-based HTTP/1.1 loop. `H2Connection` owns the binary framing
  (`http2.frames`), HPACK (`http2.hpack`), the stream state machine, flow control,
  and the required DoS limits (Rapid-Reset / CONTINUATION-flood / HPACK-bomb caps —
  `docs/TRANSPORTS.md` §6); each request stream is dispatched back into the **same**
  `send_head`/`do_POST` pipeline. No client picks `h2` → graceful fallback to
  HTTP/1.1 on the same socket. This tier adds **no** dependency: TLS+ALPN are stdlib
  `ssl`, and HPACK/framing are pure code.
- **HTTP/3 — `http3.py` (optional `servery[http3]` extra, `--http3`).** A separate
  UDP/QUIC listener backed by `aioquic`; it is imported lazily by `cli.main` only
  when `--http3` is given (raising `Http3UnavailableError` → a clean exit if the
  extra is absent), and requires TLS. When live, the TCP tiers advertise it via
  `Alt-Svc`. QUIC + QPACK + h3 framing come from `aioquic`; servery drives the loop
  and dispatches into the shared handler.
- **`_oscrypto.py` (ctypes crypto).** A thin, isolated `ctypes` binding to OS crypto
  already loaded in-process (OpenSSL `libssl`/`libcrypto`, or Windows CNG) — the
  vetted high-level AEAD/QUIC primitives, never hand-rolled crypto. It exists for the
  experimental zero-PyPI HTTP/3 path (Tier 3, `docs/TRANSPORTS.md` §4) and is **not**
  imported on the default code path; like `security.py`, the FFI boundary is kept
  small and reviewable in one place.

---

## 3. Request lifecycle

`ServeryHandler` holds a reference to its server, and thus to `Config`
(`self.server.config`). Optional features are *gated* by `Config` so the default
path (plain GET, no auth, no upload) stays minimal.

### GET / HEAD (`send_head` override)

```
handle_one_request (base)
  └─ do_GET / do_HEAD (base)  ── both call ──▶ send_head() (servery override)
       │
       ├─[1] auth gate ............ if config.auth: auth.check(self.headers)
       │                            └─ fail → 401 + WWW-Authenticate, return None
       │
       ├─[2] translate + secure ... fs_path = security.resolve(self, self.path)
       │                            └─ escape/symlink → 404 (never 403-leak), return None
       │
       ├─[3] is it a directory? ... os.path.isdir(fs_path)?
       │        ├─ no trailing '/' → 301 redirect (base behavior, restated)
       │        ├─ index file?     → fall through to file branch with index path
       │        └─ else            → return listing.render(self, fs_path)  ──▶ HTML body
       │
       ├─[4] not found? ........... apply fallbacks (in handler):
       │        ├─ SPA: serve config.spa index (if config.spa)   [rewrite, not redirect]
       │        ├─ 404.html present → serve it with 404 status
       │        └─ else → 404
       │
       ├─[5] conditional GET ...... If-Modified-Since → 304 (base logic, restated)
       │
       ├─[6] cache/CORS headers ... Cache-Control / ETag / ACAO (in handler)
       │                            (rides the extra_response_headers hook)
       │
       └─[7] send body ............ Range header present?
                ├─ yes → ranges.parse(...) → 206 + Content-Range + Accept-Ranges,
                │         seek + bounded copyfile  (416 if unsatisfiable)
                └─ no  → 200 + Content-Length, copyfile (bounded buffer)
```

Where each cross-cutting concern fires:

- **auth** — step [1], *before any path work*, so an unauthenticated client
  cannot even probe path existence.
- **path-safety** — step [2], the single choke-point (§5). Everything downstream
  receives an already-validated absolute path inside the root.
- **CORS** — preflight `OPTIONS` is its own `do_OPTIONS` (→ `204`); simple-request
  CORS headers are added at step [6].
- **SPA / clean-URL fallback** — step [4], an internal *rewrite* (no redirect),
  guarded so it does not rewrite real asset paths.

### POST (`do_POST` override — upload)

```
do_POST() (servery, only defined if config.upload)
  ├─[1] feature gate ......... if not config.upload → 405 Method Not Allowed
  ├─[2] auth gate ............ auth.check(...) (same as GET) → 401 on fail
  ├─[3] target dir ........... security.resolve(self, self.path) → must be a dir in root
  ├─[4] size precheck ........ Content-Length > config.max_upload → 413, drain/close
  ├─[5] parse body ........... upload.parse(self.rfile, content_type, content_length, ...)
  │        └─ multipart: stream each part to tempfile.NamedTemporaryFile in target dir,
  │           enforcing running byte cap; sanitize filename (basename only, no traversal)
  ├─[6] commit ............... os.replace(tmp, final)  [atomic; honors overwrite policy]
  └─[7] respond .............. 303 See Other → back to listing  (or 201/JSON for API use)
```

If `config.upload` is false, `do_POST` is **not defined on the class at all**, so
the base dispatcher returns `501 Not Implemented` automatically — the default
build literally cannot accept writes (§4, §5).

---

## 4. Composition strategy

**Recommendation: explicit method overrides on one handler class, delegating to
single-purpose helper modules — NOT a mixin tower.**

`ServeryHandler(SimpleHTTPRequestHandler)` is a single class with a handful of
overridden methods. Each override is short and reads top-to-bottom as a sequence
of calls into `auth`, `security`, `ranges`, `listing`, `upload`, and `archive`
(with header-emission helpers inline in the handler). The *features* live in
modules; the *handler* is the wiring diagram.

```python
class ServeryHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        cfg = self.server.config
        if cfg.auth and not auth.check(self, cfg.auth):
            return self._challenge()                    # 401
        fs_path = security.resolve(self, self.path)     # §5 choke-point
        if fs_path is None:
            return self._not_found()
        if os.path.isdir(fs_path):
            return self._handle_dir(fs_path)            # redirect / index / listing
        return self._send_file(fs_path)                 # range-aware, cache headers

    def do_POST(self):                                  # defined only when cfg.upload
        ...

    def translate_path(self, path):                     # base body, wrapped by security
        ...
```

### Why explicit overrides over mixins

- **No MRO archaeology.** A mixin tower (`AuthMixin`, `RangeMixin`,
  `UploadMixin`, `CorsMixin`, …) makes the *order* of cooperative `super()`
  calls load-bearing and invisible. Reordering a base list silently changes
  behavior. For a tool whose pitch is "read it in an afternoon"
  (`PRINCIPLES.md` §5), a flat handler with named steps is far more honest.
- **Feature gating is just an `if`.** With overrides, "is auth on?" is a literal
  `if cfg.auth:` at the top of a method. With mixins it becomes "is the mixin in
  the MRO?", which is decided at class-construction time and is harder to make
  conditional per-`Config`.
- **Helper modules are testable in isolation.** `ranges.parse_range()` and
  `upload.parse_multipart()` are plain functions with no handler state; they get
  unit tests without spinning a server (§7). A mixin's method is bound to
  handler internals and is awkward to test alone.

### Keeping the default path minimal

- `do_POST` / `do_OPTIONS` exist **only** when their feature is enabled. We
  build the handler class with a small factory:

  ```python
  def build_handler(config: Config) -> type[ServeryHandler]:
      ns = {}
      if config.upload:
          ns["do_POST"] = ServeryHandler._do_POST_impl
      if config.cors:
          ns["do_OPTIONS"] = ServeryHandler._do_OPTIONS_impl
      return type("ConfiguredServeryHandler", (ServeryHandler,), ns)
  ```

  This is the *one* place we synthesize a class, and it is composition by
  presence/absence of methods — not a mixin hierarchy. The base class's
  name-based dispatch (`do_<METHOD>`) makes this clean: an absent method ⇒ the
  capability simply does not exist on the wire.
- Every `send_head` branch for an optional feature is guarded by a `Config`
  flag, so a bare `servery` walks the shortest path: auth check (skipped),
  resolve, isdir → listing or → plain `Content-Length` send.

> **Anti-pattern we forbid:** adding a new feature by inserting another base into
> the MRO. New features are a new override-step or a new helper module, gated by a
> new `Config` field (§9).

---

## 5. Security architecture

Security is *centralized*, not scattered across features. Three choke-points:

### 5.1 One path-resolution choke-point

All filesystem access for a request goes through `security.resolve(handler,
url_path) -> str | None`. It is the *only* function that turns a URL into a
filesystem path, and every feature (GET, listing, archive, upload target) calls
it. It composes the base `translate_path` with a containment check:

This follows Starlette's audited `lookup_path` model (`BEST-PRACTICES.md` §3.3,
Appendix): **`realpath` BOTH sides**, then `os.path.commonpath` containment, with
absolute-path and NUL-byte rejection, failing **closed to 404**:

```python
def resolve(handler, url_path) -> str | None:
    candidate = handler.translate_path(url_path)   # base: normpath, drops .., strips //
    root = handler.server.config.root_realpath     # os.path.realpath(directory), once
    try:
        real = os.path.realpath(candidate)         # collapse symlinks on the candidate
        # containment via commonpath: separator-correct, cross-platform.
        # commonpath raises ValueError on mixed-absoluteness/different drives → fail closed.
        if os.path.commonpath([real, root]) != root:
            return None                            # client tried to break out → 404
    except ValueError:
        return None                                # NUL byte, mixed drives, etc. → 404
    if handler.server.config.no_symlinks and os.path.islink(candidate):
        return None
    return candidate
```

- We **reuse** the base `translate_path` (it already drops `..`, drive letters,
  and the `//` open-redirect) and **add** `realpath`-both-sides + `commonpath`
  containment so a symlink *inside* the root cannot point *outside* it.
- **Prefer `os.path.commonpath([real, root]) == root` over a string
  `real.startswith(root + os.sep)` check.** `commonpath` is the cross-platform,
  separator-correct comparison Starlette uses (`staticfiles.py:154-173`) and
  avoids the `/a/rootEVIL` vs `/a/root` prefix-collision class of bug.
- Reject **absolute and backslash-absolute** request paths
  (`path.startswith(("/", "\\"))`) and **catch `ValueError` from embedded NUL
  bytes** → 404, mirroring Starlette's caller. Keep the security-critical
  containment in `os.path` (not `pathlib`) — the string-level `commonpath` check
  is the audited primitive (`BEST-PRACTICES.md` §7).
- The default symlink policy is conservative: `--no-symlinks` rejects symlinks
  outright; without it, symlinks are followed but the `realpath` containment
  check still forbids escaping the root. A symlink can never become a traversal
  bypass.
- Failures (traversal, symlink-escape, permission, NUL byte) all return **404,
  never 403** — we do not leak whether a forbidden path exists, and we map
  `PermissionError` to `404` too (a dev tool need not advertise an
  exists-but-unreadable file).

### 5.2 Constant-time auth

`auth.py` parses `Authorization: Basic <b64>` (`base64.b64decode`, `latin-1`),
splits `user:pass`, and compares with `hmac.compare_digest` — **never `==`**
(the timing-leak both `uploadserver` and `tiny-http-server` shipped). Stored
credentials may be a hashed file (`user:sha256:hex`, miniserve-compatible),
hashed with `hashlib` and compared digest-to-digest, again via
`compare_digest`. Any nonce/token uses `secrets`.

### 5.3 Bind warning + Basic-Auth-over-HTTP warning

- **Default bind is `127.0.0.1`** (`config.host` default), not `0.0.0.0`. Serving
  the network is explicit opt-in; `cli.py` prints a clear warning when bound to a
  non-loopback address.
- If `config.auth` is set but `config.tls` is not, `cli.py` emits a **loud**
  startup warning: Basic Auth over plain HTTP is base64, i.e. effectively
  cleartext. We never imply privacy without TLS.

### 5.4 Upload bounds (centralized in `upload.py`)

- Off unless `config.upload`. Enforced by method-absence (§4), so a default
  build cannot write.
- A **running** byte cap (`config.max_upload`) is enforced *while streaming*, not
  just via the (spoofable) `Content-Length` — the parser aborts and deletes the
  temp file on overrun.
- Filenames are reduced to `os.path.basename` and re-validated through
  `security.resolve` against the upload target; no part may traverse out.
- Writes go to a `tempfile.NamedTemporaryFile` in the target dir, then
  `os.replace` (atomic) — never a partial file at the destination, never an
  overwrite outside the target.

All four concerns are reviewable in `security.py` + `auth.py` + the top of
`upload.py`, not sprinkled through `handler.py`.

---

## 6. Concurrency & streaming

### Concurrency model

`ServeryHTTPServer(ThreadingHTTPServer)` (and the `…HTTPSServer` variant) inherit
`socketserver.ThreadingMixIn` with `daemon_threads = True`: one thread per
connection, no event loop, no async framework (`PRINCIPLES.md` §0). This is
sufficient for a dev/LAN tool and matches the base class. An optional
connection/worker cap can gate upload concurrency, but the default is the stdlib
behavior unchanged.

### Large-file download streaming

`copyfile` is overridden to copy with an explicit bounded buffer rather than
loading files into memory:

```python
def copyfile(self, source, outputfile):
    shutil.copyfileobj(source, outputfile, length=64 * 1024)
```

The file object from `open(path, "rb")` is streamed straight to `self.wfile`; RAM
use is one buffer regardless of file size.

### Range / resumable (servery-built — stdlib has none)

`ranges.py` parses `Range: bytes=start-end` (handling open-ended `bytes=a-`,
suffix `bytes=-N`, and rejecting unsatisfiable ranges with `416` +
`Content-Range: bytes */size`). On a satisfiable single range, `send_head` emits
`206 Partial Content` with `Content-Range: bytes a-b/total` and
`Accept-Ranges: bytes`, then:

```python
f.seek(start)
remaining = end - start + 1
while remaining:
    chunk = f.read(min(64 * 1024, remaining))
    if not chunk:
        break
    self.wfile.write(chunk)
    remaining -= len(chunk)
```

Single-range only; multipart/byteranges is out of scope (small surface,
`PRINCIPLES.md` §6).

### Upload streamed to disk with a size cap

The body never lands in memory whole. `do_POST` reads from `self.rfile` (bounded
by `Content-Length`) and streams each multipart part to a `tempfile` in the
destination directory, enforcing the running cap, then `os.replace`.

### Multipart-without-`cgi` — the design decision

`cgi.FieldStorage` is gone (3.13). Two stdlib paths exist; they trade memory for
simplicity:

| Approach | How | Memory | When acceptable |
|---|---|---|---|
| **`email.parser.BytesParser`** | Read the whole body, prepend a synthetic `Content-Type: multipart/form-data; boundary=…` header, `email.message_from_bytes`, walk `msg.iter_parts()`, `part.get_payload(decode=True)`. | **Buffers entire body in RAM.** | Small/bounded uploads where `max_upload` is comfortably below available RAM. Simple, obviously-correct, ~15 lines. |
| **Hand-rolled streaming boundary parser** | Read `rfile` in fixed chunks, scan for the `--boundary` delimiter across chunk seams, write part bodies directly to a tempfile as they arrive, parse per-part headers with `email.parser` on just the (small) header block. | **One buffer; constant memory.** | The general case — large files, the "stream to disk" property the old `make_file()` hook gave us. |

**Recommendation: ship the streaming boundary parser as the default**, writing to
`tempfile.NamedTemporaryFile(dir=target, delete=False)` and committing with
`os.replace` for atomicity. It preserves the bounded-memory and atomic-write
properties that make upload safe on a small host, and it is the honest answer to
losing `cgi`. The `email.parser` in-memory path is documented as the simpler
fallback and is acceptable only when `max_upload` is small by configuration.

Design details of the streaming parser:

- The boundary comes from the `Content-Type` header's `boundary=` parameter
  (parsed with `email.message.Message`/`email.policy`, **not** the removed
  `cgi.parse_header`).
- Per-part headers (`Content-Disposition`, `Content-Type`) are a small block
  ending in `\r\n\r\n`; parse that block alone with `email.parser.BytesParser`
  (in-memory is fine — it is bytes of headers, not the payload).
- The body scanner must handle the delimiter straddling a read boundary: keep a
  tail of `len(boundary)+4` bytes between chunks.
- On any overrun of `max_upload`, on a missing final boundary, or on a traversal
  filename, abort: delete the temp file, send `413`/`400`, and stop reading.
- `urllib.parse.parse_qsl` handles non-multipart `application/x-www-form-urlencoded`
  bodies (e.g. simple form fields).

### Archive streaming

`archive.py` streams a directory to the socket: `tarfile.open(fileobj=self.wfile,
mode="w|gz")` is genuinely streaming (the `|` mode never seeks), so a `.tar.gz`
download needs no temp file and no `Content-Length` (chunked / connection-close).
Zip (`zipfile`, which needs seekable output for the central directory) writes to
a `tempfile` then streams — or uses a streaming-zip chunking approach — accepting
the same no-`Content-Length` tradeoff.

---

## 7. Testing strategy

**stdlib `unittest` only — no pytest** (`PRINCIPLES.md` §0 applies to dev deps in
spirit; the test suite must run on a bare interpreter).

### Spinning a real server

The core fixture binds `ServeryHTTPServer` to an **ephemeral port** (`("127.0.0.1",
0)`), reads the actual port from `httpd.server_address`, runs `serve_forever` in a
daemon `threading.Thread`, and tears down with `shutdown()` + `server_close()` in
`tearDown`. Requests go out via `http.client.HTTPConnection` (or `urllib.request`)
— real sockets, real HTTP, no mocking of the handler.

```python
class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        cfg = Config(directory=self.root.name, host="127.0.0.1", port=0)
        self.httpd = make_server(cfg)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        self.thread.join(timeout=5); self.root.cleanup()
```

### Fixtures

`tempfile.TemporaryDirectory` for the served root; populate with known files,
sizes, and subdirs per test. Symlink-escape tests create a symlink inside the
root pointing outside it.

### Coverage map

- **Listing** — GET `/`; assert sizes, mtimes, sort links; assert `?C=S&O=D`
  reorders; assert `html.escape` of hostile filenames.
- **Range** — GET with `Range: bytes=0-9` → `206`, `Content-Range`, exact bytes;
  `bytes=-5` suffix; `bytes=5-` open; an unsatisfiable range → `416`.
- **Path-traversal regression** — the security suite is non-negotiable: GET
  `/../../etc/passwd`, percent-encoded `%2e%2e%2f`, absolute paths, and a symlink
  escaping the root all return `404`. These run on `security.resolve` *and*
  through a live server.
- **Auth** — no header → `401` + `WWW-Authenticate`; wrong creds → `401`; correct
  → `200`. A timing assertion is impractical to make reliable; instead a unit
  test asserts `auth.check` calls `hmac.compare_digest` (or simply that `==` never
  appears) — guarding the constant-time property structurally.
- **Upload** — POST multipart to an `--upload` server: file lands in target,
  correct bytes, atomicity (no partial on mid-stream abort), `413` on
  over-`max_upload`, traversal filename rejected. A separate unit test feeds
  crafted multipart bytes straight to `upload.parse_multipart` (no server).
- **TLS** — start `ServeryHTTPSServer` with a throwaway cert/key generated into a
  `tempfile` (via the `ssl`/`cryptography`-free path: ship a tiny fixture cert, or
  generate with `openssl` in a test-only helper guarded by availability); connect
  with an `ssl.SSLContext` that trusts it; assert HTTPS round-trip. Skip cleanly
  if cert generation is unavailable.
- **Composition** — assert a default `Config` produces a handler class with **no**
  `do_POST`/`do_OPTIONS`, and that an `upload=True` config does.

`python -m unittest discover` is the entire test command.

---

## 8. Packaging & entry points

Pure `pyproject.toml`, **empty base `[project.dependencies]`** (the property that
*defines* servery), src-layout, modern build backend (stdlib-adjacent
`setuptools`/`hatchling` as a build-time-only backend — not a runtime dep). The
**one** optional extra is the HTTP/3 tier (`servery[http3]` → `aioquic`), declared
under `[project.optional-dependencies]` and pulled in only on explicit opt-in; the
HTTP/2 tier needs no extra (it is pure stdlib).

```toml
[project]
name = "servery"
requires-python = ">=3.13"
dependencies = []                       # the core — empty, forever

[project.optional-dependencies]
http3 = ["aioquic"]                     # opt-in HTTP/3 tier only; imported under --http3

[project.scripts]
servery = "servery.cli:main"            # console script

[build-system]
requires = ["hatchling"]                # build-time only; not installed at runtime
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/servery"]
```

Three entry points, one behavior (`PRINCIPLES.md` §4):

- **`python -m servery`** → `src/servery/__main__.py` → `cli.main()`.
- **`servery`** console script → `servery.cli:main`.
- **`import servery`** → `serve(config)`, `Config`, `make_server(config)`,
  `ServeryHandler` exposed from `__init__.py`.

`cli.main()` does exactly: `argparse` → `Config` (`config.from_args`) →
`make_server(config)` → print banner/warnings → `serve_forever()`. The CLI is a
thin view over the library; everything the flags do is reachable by constructing
a `Config` in Python.

---

## 9. Extensibility seam

servery is **not** a framework and grows no plugin/route API (`VISION.md` §5,
`PRINCIPLES.md` §2). The honest extensibility story is for *the project's own
maintainers*, not for end-user app-building:

**A new feature is exactly three things, in order:**

1. **A `Config` field** (the gate — default off if it changes behavior or
   touches safety).
2. **A helper module or function** (`feature.py` or a function in an existing
   single-purpose module) — pure, stdlib-only, unit-testable without a server.
3. **A call site**: one new guarded step in `send_head`/`do_POST`, or a new
   `do_<METHOD>` synthesized by `build_handler` when the gate is on.

It must pass the `PRINCIPLES.md` §7 scope rubric *before* any of that:
zero-dependency gate (absolute) → file-server-lane gate → safe-default gate →
smallness gate. Concretely: WebDAV would be a `do_PROPFIND` built with
`xml.etree.ElementTree` (zero-dep, but deferred for smallness); QR codes and full
Markdown **fail the zero-dependency gate** and are out — not faked, not vendored.

For *embedders*, the seam is the library surface itself: build a `Config`,
optionally subclass `ServeryHandler` to override one method, and hand the class
to `make_server` or drop it into your own `socketserver` setup. That is the
extent of the extension surface — by design. The moment a change would add a
route table, an app object, a middleware chain, or a third-party import, it is out
of scope and the answer is no.

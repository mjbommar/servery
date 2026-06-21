# servery — Principles & Tenets

These are the rules servery lives by. They are deliberately opinionated. When a
design decision is unclear, re-read Principle 0; it usually settles the
question, and where it does not, the scope rubric in §7 does.

---

## 0. Zero dependencies. Pure standard library. Forever.

**The servery CORE has zero third-party (PyPI) runtime dependencies and depends
only on the Python standard library. This is non-negotiable and outranks every
other principle here.** It is the soul of the project, not a nice-to-have.

If `pip install servery` pulls in anything other than servery itself, we have
failed — no matter how good the feature was.

**Refinement (transport tiers).** The zero-PyPI mandate is on the **core**. The
optional, opt-in HTTP/2 and HTTP/3 transport tiers (`docs/TRANSPORTS.md`) **may**
use vetted libraries behind extras (`servery[http2]`, `servery[http3]`) — but only
after preferring two cheaper sources first: a stdlib path, and **binding
already-present OS libraries via `ctypes` (stdlib) rather than adding a PyPI
dependency** (e.g. system OpenSSL `libcrypto`/`libssl` or Windows CNG for QUIC
crypto). The order of preference is therefore: **stdlib → OS library via `ctypes`
→ vetted PyPI extra (explicit opt-in only).** A bare `pip install servery` stays
empty-`dependencies` forever; the core never imports any of that.

This is the *point*, not a constraint we tolerate. The entire value proposition
is "you already have everything you need." Every dependency we could add is a
dependency the user could have added themselves to `http.server`; adding it for
them is not the product.

**The rule:** when a desirable feature appears to need a dependency, the answer
is one of exactly three things, in order of preference:

1. **Find a stdlib path.** The standard library is enormous —
   `ssl`, `hmac`, `secrets`, `email`, `urllib`, `mimetypes`, `socketserver`,
   `http`, `base64`, `hashlib`, `gzip`, `zipfile`, `tarfile`, `json`, `html`,
   `string.Template` — most of what a file server needs is already there.
2. **Scope the feature down** to the part that *is* reachable with the stdlib,
   and document the boundary.
3. **Drop the feature.** A missing feature is cheaper than a betrayed promise.

**Never** add a third-party dependency. There is no "just this once."

### Consequences we accept on purpose

The zero-dep mandate has real, sharp consequences. We name them up front so
nobody re-litigates them later:

- **No Markdown rendering.** The stdlib has no Markdown parser. So
  README-as-rendered-HTML (a thing miniserve does) is out of scope. The most we
  will do is serve/show README files as **escaped plaintext**. We will not
  vendor or reimplement a Markdown parser to close this gap.
- **No `cgi` / `cgi.FieldStorage` for uploads.** The `cgi` module was **removed
  in Python 3.13**. Since we target 3.13+ (see §3), `cgi` is simply not
  available to us, and we would not use it even on older interpreters. Therefore
  **multipart/`form-data` upload parsing must be hand-rolled** — boundary
  splitting against the `Content-Type` boundary parameter, with per-part headers
  parsed via `email.parser` / `email.message`, and `urllib.parse.parse_qsl` for
  simple url-encoded forms. This is a deliberate, owned piece of code, not an
  accident. It must be written carefully (streaming where possible, strict on
  boundaries, bounded in memory).
- **No async framework, no template engine, no rich-text/UI toolkit.** Our
  listing UI is server-rendered HTML/CSS built with stdlib string tooling
  (`html.escape`, `string.Template`), shipped inline. No build step, no asset
  pipeline.
- **We build on the real stdlib base.** servery extends
  `http.server.SimpleHTTPRequestHandler` and serves via
  `ThreadingHTTPServer` / `ThreadingHTTPSServer` (the stdlib already gives us
  threading, HTTPS via `ssl`, `If-Modified-Since` handling, and directory
  redirects). We add what stdlib lacks — rich listing, auth, upload, and
  `Range` support (stdlib does **not** honor `Range` today) — rather than
  reinventing the HTTP plumbing.

## 1. Safe by default, honest about limits

servery is a dev / LAN / ad-hoc-sharing tool. We will not pretend otherwise.
But we can still be meaningfully *safer than the stdlib out of the box*:

- **Bind to localhost by default.** Serving the whole network is an explicit,
  opt-in choice (`--host 0.0.0.0`), not the default. `http.server`'s historical
  default of binding broadly is a footgun we decline to inherit.
- **Path-traversal protection.** No request path may escape the served root.
  Resolve and verify every translated path stays within the configured
  directory; reject `..`, encoded traversal, and absolute-path tricks.
- **Careful symlink handling.** Decide explicitly (and configurably) whether
  symlinks may point outside the root; default to *not* following links out of
  the served tree. Never let a symlink become a traversal bypass.
- **Constant-time auth comparison.** Compare credentials with
  `hmac.compare_digest`, never `==`. Generate any tokens/nonces with `secrets`.
- **Loud about HTTP Basic Auth without TLS.** Basic Auth over plain HTTP sends
  credentials in base64 (i.e. effectively in the clear). If auth is enabled
  without TLS, we **warn loudly** at startup. We never imply auth-over-HTTP is
  private.
- **Upload is opt-in and bounded.** Writing files is off unless explicitly
  enabled. When on, enforce size limits, refuse path traversal in filenames,
  and never overwrite outside the upload target.

**Out of scope:** production hardening — rate limiting, WAF behavior,
DoS resistance, hostile-internet exposure, CSRF frameworks, multi-tenant
isolation. The honest posture: *safe defaults for trusted networks; put a real
reverse proxy in front of it if you need more.* We keep `http.server`'s spirit
of "not for production," but we move the safe-default needle as far as the
stdlib lets us.

## 1a. Standards-compliant by default (RFC 9110 / 9111 / 9112)

servery is a **conformant HTTP/1.1 origin server**, not an HTTP/1.0 toy. Where
the stdlib base is RFC 2616-era and HTTP/1.0-by-default, servery closes the gap
to modern HTTP semantics with the stdlib alone:

- **HTTP/1.1 with persistent connections** (`protocol_version = "HTTP/1.1"`),
  honoring `Connection: close`, with every streamed body correctly framed
  (chunked or `Connection: close`) — RFC 9112.
- **Correct conditionals and validators**: the full `If-Match` /
  `If-Unmodified-Since` / `If-None-Match` / `If-Modified-Since` precedence ladder,
  a weak `ETag`, `304`/`412` with validator echo, and `Range`/`206`/`416` —
  RFC 9110 §13/§14, §8.8.
- **Correct caching, dates, and metadata**: `Cache-Control`, IMF-fixdate `Date`,
  `Content-Type`/`Content-Length`, `Content-Disposition` with RFC 6266/8187
  filenames.

The map of exactly what each RFC requires, what the base already gives us, and
what servery adds lives in `STANDARDS.md`. **This principle is subordinate to
Principle 0:** any compliance target reachable only by adding a *core* dependency
is out of the core and recorded as such. The **core** therefore speaks HTTP/1.1
and its TLS ALPN advertises only `http/1.1`. **HTTP/2 (RFC 9113) and HTTP/3 (RFC
9114) are not part of the zero-dep core, but they are no longer flatly out:** they
are optional, opt-in **transport tiers** (`docs/TRANSPORTS.md`) — h2 is feasible in
pure stdlib (the preferred path) with an optional `h2` backend; h3 is offered via
`aioquic` or an experimental `ctypes`→OpenSSL ≥ 3.5 native backend. ALPN/`Alt-Svc`
advertise `h2`/`h3` **only** when the corresponding tier is enabled. Standards
conformance never outranks the zero-dep mandate; it is what we achieve *within* it.

## 1b. Secure web-facing defaults

servery renders HTML listings containing user-controlled filenames, so it is an
XSS sink by construction; its defaults must be safe for a web-facing surface
out of the box — not opt-in hardening:

- **`X-Content-Type-Options: nosniff` on every response** (a `.txt` must not be
  sniffed into `text/html`).
- **Context-correct output escaping**: `html.escape(name)` with `quote=True` for
  every value that could land in HTML text *or* an attribute, `urllib.parse.quote`
  for URL targets, and control-character stripping in filenames — never the base's
  `quote=False`.
- **Defense-in-depth headers** on servery-**generated** pages: a tight
  `Content-Security-Policy` (listings/error pages only, never on served user
  HTML), `Referrer-Policy`, and `Strict-Transport-Security` **only under TLS**.
- **Safe operational defaults**: a per-request socket **timeout** (Slowloris
  mitigation) on by default; fail-closed path resolution (404, never a 403 leak).

These are all `send_header`/stdlib calls — zero-dep — and **on by default**, with
a `--no-security-headers` escape hatch. This principle **does not** promise
production hardening: rate limiting, WAF behavior, DoS resistance, and CSRF
frameworks remain out of scope (Principle 1) — the honest posture is "safe
defaults for trusted networks; front it with a reverse proxy for exposure."
Like Principle 1a, it is subordinate to Principle 0: every default here is
reachable with the standard library alone.

## 2. File server, not framework — scope discipline

servery serves a directory. It does not help you build an application.

- **No user-defined routes or handlers**, no app object, no middleware system,
  no plugin API for request dispatch. If the request is "let me add an
  endpoint," it is the framework lane (Flask/Bottle) and the answer is no.
- The mental model never grows beyond **{directory, files, listing, browser}**
  plus the four niceties (rich listing, auth, upload, HTTPS).
- Internal abstractions (our own listing template, our own upload parser) are
  fine. *Exposing* them as an extension surface for building apps is not.

This discipline is what keeps servery finishable and honestly describable. The
moment we add routing, we are a worse Flask; we will not.

## 3. Python version support policy

**Minimum supported Python: 3.13.** We track the CPython upstream support
window and support every non-EOL CPython at or above our minimum.

Rationale (as of mid-2026):

- Python **3.9 is already EOL** (October 2025). **3.10 and 3.11 reach EOL in
  October 2026** — i.e. within months of this writing. Building a brand-new
  project on versions that are EOL or about to be is poor stewardship.
- The **`cgi` module was removed in 3.13.** Targeting 3.13+ means we live in the
  post-`cgi` world *natively*: we hand-roll multipart parsing once, for the
  versions we actually support, instead of carrying a conditional `cgi` path for
  legacy interpreters. The constraint and the floor reinforce each other.
- 3.13 began the new support cadence (two years full support, three years
  security) and is supported through **October 2029**, giving servery a long
  runway without us chasing a moving floor.
- A young file-server project gains little from supporting interpreters its
  users are being told to leave. We would rather use modern stdlib cleanly than
  straddle five-year-old versions.

**Policy mechanics:**

- We declare `requires-python = ">=3.13"`.
- We test against every supported CPython from the floor up (3.13 and each newer
  release as it ships).
- We raise the floor only deliberately, in a minor release, with a note in the
  changelog — never silently.
- *(Open question for the requirements authors: if a concrete user need for
  3.11/3.12 emerges, is the cost of a hand-rolled-multipart-only backport worth
  lowering the floor? Default answer today: no.)*

## 3a. Free-threading is a first-class target

servery must run correctly and well on the **free-threaded (no-GIL) CPython
builds** (3.13t / 3.14t), not merely tolerate them. A threaded file server is
exactly the workload free-threading is meant to speed up, and the multiplexing
HTTP/2 backend (`docs/TRANSPORTS.md`) makes thread-safety load-bearing rather than
incidental.

- **No module-level mutable state.** Shared state lives on the `Config` (frozen)
  or on per-request/per-connection objects — never in module globals that
  concurrent threads could race.
- **Do not rely on the GIL for correctness.** Anything previously "safe because
  the GIL made it atomic" (dict mutation, counter increments, lazy caches) must be
  made explicitly safe — immutable, thread-local, or guarded by a lock — because
  on a free-threaded build that atomicity is gone.
- **Test on free-threaded builds.** The suite (`ARCHITECTURE.md` §7) runs on
  3.13t/3.14t in CI alongside the default builds; concurrency-sensitive paths
  (listing, range, upload, and any h2 stream table) get explicit multi-threaded
  tests.

This is subordinate to Principle 0 — it is reached with the stdlib alone
(`threading`, `concurrent.futures`) — and reinforces Principle 5: code that holds
no hidden shared state is also the code that is easiest to read and hack.

## 4. CLI-and-importable ergonomics

servery is equally a command and a library. Neither is an afterthought.

- **Three entry points, same behavior:** `python -m servery`, the `servery`
  console script, and `import servery`. The module and the script are thin
  wrappers over the same public API.
- **The library is the product, the CLI is a view of it.** Configuration lives
  in plain objects/params; the CLI parses argv (via stdlib `argparse`) into
  exactly those params. Anything you can do from the command line you can do from
  Python.
- **Sensible zero-config defaults.** Bare `servery` serves the current directory
  on localhost with the rich listing and no auth/upload/TLS. Every nicety is one
  obvious flag away.
- **Composable for embedders.** Someone should be able to `import servery`,
  construct a handler/server, and drop it into their own `socketserver` setup —
  without us having become a framework to allow it.
- **Friendly failure.** Clear startup messages (bound address, whether auth/TLS
  are on, the loud no-TLS-auth warning). Errors say what to do, not just what
  broke.

## 5. Boring, readable, hackable

Because we cannot reach for dependencies, our own code is the asset. It must be
the kind of pure-Python that a user could read in an afternoon and patch
themselves — which is, after all, why they chose a pure-Python tool. Prefer
clarity over cleverness; prefer the obvious stdlib call over a hand-optimized
trick.

## 6. Stable, small surface

A small tool earns trust by not churning. Keep the public API and CLI flags
small and stable; deprecate slowly and loudly; treat each new flag as a cost.
Every feature we *don't* add is a feature we never have to maintain, document,
or secure.

## 7. The scope rubric — how we decide if a feature is in

Run every proposed feature through this filter, **in order**. A feature must
pass *all* gates to be in scope.

1. **Zero-dependency gate.** Can it be built with the standard library alone,
   without vendoring a parser/engine/toolkit? If no → **out** (or scope it down
   to the stdlib-reachable subset). This gate is absolute; nothing overrides it.
2. **File-server-lane gate.** Is it about *serving/sharing a folder*, or is it
   about *building an application* (routes, app logic, dispatch)? If it's
   framework-lane → **out**.
3. **Safe-default gate.** Does it preserve safe-by-default behavior, or does it
   push the project toward "production web server" promises we won't keep? If it
   degrades the safe default with no opt-in → **out**, or **redesign** until the
   risky behavior is explicit opt-in.
4. **Smallness gate.** Does the benefit justify the permanent maintenance,
   documentation, and security surface? When in doubt → **out**; the default
   answer to "should we add this?" is **no**.

Worked examples:

- *Sortable listing with sizes/dates* → stdlib (`os.scandir`, `datetime`,
  string templating) ✅, file-server-lane ✅, safe ✅, clearly justified ✅ →
  **in** (it's the headline feature).
- *Basic auth* → stdlib (`base64`, `hmac.compare_digest`) ✅, file-server-lane
  ✅, safe-by-default with loud no-TLS warning ✅ → **in**.
- *Upload* → stdlib (hand-rolled multipart via `email.parser`) ✅,
  file-server-lane ✅, safe *if* opt-in + bounded + traversal-checked ✅ → **in,
  opt-in**.
- *HTTPS* → stdlib (`ssl`, `ThreadingHTTPSServer`) ✅ → **in**.
- *Range requests* → stdlib ✅, file-server-lane ✅ (stdlib lacks it; we add it)
  → **in**.
- *Markdown README rendering* → needs a non-stdlib parser ✗ → **out** (escaped
  plaintext at most).
- *User-defined routes / app endpoints* → framework-lane ✗ → **out**.
- *Built-in TLS via a vendored crypto lib* → dependency ✗ → **out** (use stdlib
  `ssl`; that's the only TLS we ship).

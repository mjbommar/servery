# Vision & goals

> A batteries-included `python -m http.server`.

**servery** is a zero-dependency, pure-Python (standard-library-only) HTTP file
and application server. Its default remains the simple directory server: rich
listings, optional auth and upload, and HTTPS without a runtime dependency tree.
Its production target is one directly exposed service on one Linux host that
owns TLS, worker lifecycle, overload behavior, and operational status without
requiring nginx, Caddy, or an external process manager.

You can run it three ways: `python -m servery`, a `servery` console script, or
`import servery` from your own code. It is `pip install servery` away, and its
default/core install has **zero third-party runtime dependencies**. Explicit
transport extras may add a small, documented dependency set; today HTTP/3 uses
`aioquic`.

---

## 1. Problem statement

Everyone who has Python reaches for the same reflex when they need to share a
folder, hand a colleague a build artifact, or sanity-check a static site:

```
python -m http.server 8000
```

It is glorious because it is *already there*. But the moment you use it for real
work, the gaps show:

- The directory listing (`SimpleHTTPRequestHandler.list_directory`) is a bare
  `<ul>` of links. **No file sizes, no modification dates, no sorting, no
  search.** You cannot tell a 2 KB file from a 2 GB one without clicking.
- **No authentication.** Anyone who can reach the port can read everything.
- **No upload.** It is download-only; receiving a file means firing up something
  else.
- **No HTTPS** without hand-rolling an `ssl` context and wiring it into the
  server yourself.
- **No range-request support.** As of CPython today, stdlib `http.server` does
  not honor the `Range` header, so large-file resume and media seeking do not
  work out of the box (see the long-standing CPython issue tracking this).
- It carries a permanent, prominent **"not for production use"** warning — and
  rightly so.

So people leave Python. They install a Rust binary (miniserve), run a Node
package (`npx serve`, `http-server`), or pull in a Flask-based tool (`updog`).
Each solves the listing/auth/upload problem — by adding a runtime, a toolchain,
or a dependency tree that the original one-liner never had.

## 2. The gap, and the evidence

The polished folder-serving tools fall into two camps, and **none of them is
both pure-Python and zero-dependency**:

| Tool | Language / runtime | Zero deps? | Rich listing | Auth | Upload | HTTPS |
|------|--------------------|:----------:|:------------:|:----:|:------:|:-----:|
| `python -m http.server` | Python stdlib | ✅ | ❌ (plain `<ul>`) | ❌ | ❌ | ❌ (manual `ssl`) |
| `uploadserver` (PyPI) | Python, on stdlib | ✅* | ❌ (inherits plain listing) | basic | ✅ | ✅ | 
| `tiny-http-server` | Python, on stdlib | ✅* | ❌ (inherits plain listing) | basic | ✅ | ✅ |
| `updog` | Python + **Flask** | ❌ | ✅ | basic | ✅ | ✅ |
| miniserve | **Rust** binary | ✅ (single binary) | ✅ | ✅ | ✅ | ✅ |
| `serve` / `http-server` | **Node.js** | ❌ (npm tree) | ✅ | partial | varies | ✅ |

\* Pure-stdlib Python tools improve on auth/upload/TLS but **inherit
`http.server`'s plain, unsortable listing** — they bolt features onto the
handler without replacing the listing UI.

The whitespace is precise:

> **No existing zero-dependency, pure-Python tool combines a rich sortable
> directory listing _and_ basic auth _and_ upload _and_ HTTPS.**

That is the exact spot servery occupies. We are not "another file server"; we
are the *Python-native* one that finally has the listing the Rust tool has,
without leaving the Python you already have installed.

## 3. Target users & use cases

servery is for people who have Python and a folder and a few minutes:

- **Developers sharing build output / artifacts** with a teammate over the LAN —
  who want sizes and dates in the listing and maybe a password.
- **Anyone doing ad-hoc file transfer** between two machines on a trusted
  network ("send me that file" / "grab this from me") — who needs *upload*, not
  just download.
- **Static-site authors** doing a quick local preview, who want range requests
  so media and large assets behave, and a listing that does not embarrass them.
- **Sysadmins / ops on a locked-down host** where installing a Rust binary or a
  Node toolchain is friction or forbidden, but Python is already present.
- **People who write `python -m http.server` reflexively** and have wished, once
  per use, that it were just a little nicer. That is the whole audience.

Representative one-liners we want to feel obvious:

```
servery                          # serve cwd, localhost, rich listing
servery ./dist --port 8080       # share a build directory
servery --auth alice:s3cret      # gate it behind basic auth
servery --upload                 # let the other side send files back
servery --tls cert.pem key.pem   # serve over HTTPS
```

## 4. Positioning: server, not framework

There are two different jobs in a Python web deployment:

- **The file-server lane** (miniserve, `npx serve`, `http-server`,
  `uploadserver`): *point me at a folder and serve it.* The mental model is a
  directory, files, and a browser. **servery is here.**
- **The application-framework lane** (Django, Flask, Starlette, FastAPI): help
  users build routes and application logic. **servery is not a framework.** It
  can host one WSGI or ASGI application supplied by the operator, just as a
  production server hosts an application without defining its routes.

If a feature request starts with "I want servery to define my application
endpoint," it belongs to the framework lane and is out of scope. Correctly
hosting an operator-provided WSGI/ASGI application, supervising it, and exposing
readiness are server responsibilities and are in scope.

Compared to the neighbors:

- **vs. `http.server`** — same spirit and same zero-install promise, but with
  the listing/auth/upload/TLS niceties stdlib will likely never grow.
- **vs. `uploadserver` / `tiny-http-server`** — same zero-dependency, pure-Python
  values, but we *replace* the listing instead of inheriting the plain one.
- **vs. miniserve** — comparable feature set, but no Rust toolchain or binary to
  distribute; you get it through `pip` and it is hackable Python.
- **vs. `serve` / `updog`** — no Node runtime, no Flask, no dependency tree.

## 5. Non-goals (explicit)

servery will **not**:

- Be a **web framework**: no servery-defined application routes, app object,
  middleware system, or application templating. Hosting one WSGI or ASGI app is
  explicitly a server feature.
- Claim the current build is production-ready before the release checklist in
  `design/production-edge-execution-backlog.md` passes. The selected target is a
  hardened, directly exposed single-service edge, but production status is an
  evidence gate rather than an aspiration.
- Require a separate public edge or process supervisor. The production profile
  must own its public sockets, local worker lifecycle, TLS, and operational
  endpoints. An operator may still place it in a larger architecture, but that
  is not the product's safety story.
- Treat ad-hoc self-signed certificates as publicly trusted. The current narrow
  zero-dependency ACME path acquires and caches certificates; unattended
  production additionally requires scheduled renewal, atomic replacement,
  retry, expiry reporting, and hot TLS reload.
- Add **third-party dependencies to the default/core install**. A narrowly
  scoped opt-in transport extra must justify its dependency and native-code
  surface under `PRINCIPLES.md`.
- Render Markdown **to GFM fidelity**, or highlight code to Pygments' breadth.
  The stdlib gives us neither a Markdown parser nor a lexer library, and we will
  not vendor one. The opt-in `--preview` renderer is a deliberate *subset* —
  enough to read a README or a source file in the browser, honestly documented
  as such in `PRINCIPLES.md`, and never on by default.
- Be a **WebDAV server, an S3 gateway, a media transcoder, a sync engine, or a
  general reverse proxy.** Those are different products.
- Pursue **multi-user accounts, roles, sessions, or a database.** Auth is a
  single shared credential gate, nothing more.

## 6. What success looks like

Success has two honest tiers. The default succeeds when a Python developer can
replace `python -m http.server` without inheriting a dependency tree or unsafe
network defaults. The production profile succeeds only when one documented
command and configuration can operate a directly exposed HTTPS static, WSGI, or
ASGI service through overload, worker crashes, reloads, and certificate renewal.
The exact first-release scope and measurable gates live in
`design/production-edge-execution-backlog.md`.

The implementation is Python, whose language-level memory safety substantially
reduces one class of server defects. CPython, OpenSSL, the operating system, and
optional native dependencies remain part of the executable stack; servery does
not claim that every layer is implemented in a memory-safe language.

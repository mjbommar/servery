# servery — Vision

> A batteries-included `python -m http.server`.

**servery** is a zero-dependency, pure-Python (standard-library-only) HTTP file
server. It serves a directory over HTTP with the niceties people actually expect
in 2026 — a rich, sortable directory listing, optional basic auth, file upload,
and HTTPS — while keeping the single property that makes `http.server` so
beloved: nothing to install but Python itself.

You can run it three ways: `python -m servery`, a `servery` console script, or
`import servery` from your own code. It is `pip install servery` away, and it
has **zero third-party dependencies, forever**.

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

## 4. Positioning: the file-server lane

There are two lanes for "small Python web tools," and servery lives firmly in
one of them.

- **The file-server lane** (miniserve, `npx serve`, `http-server`,
  `uploadserver`): *point me at a folder and serve it.* The mental model is a
  directory, files, and a browser. **servery is here.**
- **The web-framework lane** (Flask, Bottle, the `quickserve` PyPI package):
  *help me build an application* with routes, handlers, templates, and request
  dispatch. **servery is emphatically NOT here.**

If a feature request starts with "I want to add an endpoint that…", it belongs
to the framework lane and is out of scope. If it starts with "when I'm serving a
folder, I wish it also…", it may belong to servery. Keeping this line crisp is
how servery stays small, finishable, and honestly describable as "a
batteries-included `http.server`" rather than "a worse Flask."

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

- Be a **web framework**: no user-defined routes, no app object, no middleware
  system, no templating-for-your-app. (Internal templating for our own listing
  UI is fine; exposing one is not.)
- Be a **production-grade public web server**. Like `http.server`, it is a dev /
  LAN / ad-hoc-sharing tool. We aim for *safe defaults*, not hardened
  internet-facing operation. Put it behind a real reverse proxy if you must
  expose it.
- Add **third-party dependencies** for any feature, ever. (See `PRINCIPLES.md`.)
- Render **arbitrary Markdown**. The stdlib has no Markdown parser, so README
  rendering is out of scope beyond, at most, escaped plaintext. We will not
  vendor a parser to get there.
- Be a **WebDAV server, an S3 gateway, a media transcoder, a sync engine, or a
  general reverse proxy.** Those are different products.
- Pursue **multi-user accounts, roles, sessions, or a database.** Auth is a
  single shared credential gate, nothing more.

## 6. What success looks like

Success is when a Python developer who today types `python -m http.server` types
`python -m servery` instead and never thinks about it again — because it
installed with nothing extra, started just as fast, bound somewhere safe by
default, and showed a listing with sizes, dates, and sorting that they did not
have to apologize for. When they need a password, an upload box, or HTTPS, those
are one flag away rather than a different tool away. servery wins not by being
the most powerful folder server in any language, but by being the one that
finally makes the *Python you already have* good enough that you stop reaching
past it — while never asking you to install a single thing beyond Python itself.

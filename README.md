# servery

[![CI](https://github.com/mjbommar/servery/actions/workflows/ci.yml/badge.svg)](https://github.com/mjbommar/servery/actions/workflows/ci.yml)
[![Security](https://github.com/mjbommar/servery/actions/workflows/security.yml/badge.svg)](https://github.com/mjbommar/servery/actions/workflows/security.yml)
[![Docs](https://img.shields.io/badge/docs-mjbommar.github.io%2Fservery-teal)](https://mjbommar.github.io/servery/)
![Python](https://img.shields.io/badge/python-3.13%2B-blue)
![Core dependencies](https://img.shields.io/badge/core%20dependencies-zero-brightgreen)

A **zero-dependency, pure-Python** HTTP file server — *a batteries-included `python -m http.server`*.

**Run it right now — no install:**

```bash
# one self-contained file, straight from a pipe (latest release):
curl -fsSL https://github.com/mjbommar/servery/releases/latest/download/servery.py | python3 - ./public -p 8000

# …or from PyPI with uv:
uvx servery ./public --port 8000
```

The piped `servery.py` is the released package amalgamated into one auditable file
(pure stdlib). It runs code, so inspect it first if you like (`curl -fsSL <url> | less`),
pin a version (`…/releases/download/v1.2.0/servery.py`), or grab the `servery.pyz` zipapp —
both are attached to [every release](https://github.com/mjbommar/servery/releases/latest).

Serve or share a directory over HTTP with the niceties people expect from tools like
[miniserve](https://github.com/svenstaro/miniserve) or `npx serve` — rich sortable
directory listings, file upload, HTTP Basic Auth, HTTPS, range/resumable downloads, on-the-fly
archive download, even **HTTP/2** — while the core depends on **nothing but the Python standard
library**.

```console
$ servery                                  # serve the current directory on http://127.0.0.1:8000
$ python -m servery ./public --port 9000
$ servery --upload --auth me:secret        # password-protected drop box
$ servery --tls-cert cert.pem --tls-key key.pem --http2   # HTTPS + HTTP/2
```

## Features

- **Rich directory listings** — sizes, modified times, directories first, fully escaped; sortable
  columns (`?C=&O=`, Apache convention), a `?q=` name filter, a `?ext=` file-type facet, a
  breadcrumb trail, per-type icons, relative timestamps, inline size bars, an aggregate metrics
  strip, a pure-SVG modification timeline, per-file download (`?download=1`), pagination, and a
  cookie-backed light/dark/auto theme — all server-side with **no JavaScript**.
- **Correct downloads** — RFC 9110 `Range`/`206` (resumable), strong `ETag`s, the full
  conditional-request ladder (`If-None-Match`/`If-Modified-Since`/`If-Range` → `304`/`412`),
  and zero-copy `sendfile`.
- **HTTPS** — bring your own cert (`--tls-cert`/`--tls-key`) or get an ad-hoc
  self-signed one with **`--tls-self-signed`** (zero-dependency, generated at
  startup — handy for a quick encrypted LAN share). ALPN + HSTS over TLS.
- **Automatic HTTPS (Let's Encrypt), zero-dependency** — `--acme example.com` obtains
  a browser-trusted certificate over ACME HTTP-01 (RFC 8555) and serves it. The JWS +
  CSR are built from servery's own RSA/DER primitives, so trusted auto-TLS needs **no**
  third-party crypto. Staging by default; `--acme-production` for real certs.
- **HTTP Basic Auth** — single credential or a pre-hashed `user:sha256:…`, constant-time compare.
- **Upload** — opt-in `--upload`, streaming `multipart/form-data` (no `cgi`), atomic writes,
  bounded size, overwrite off by default.
- **Archive download** — stream any directory as `tar.gz` or `zip` (`?archive=tar.gz`), or
  tick the per-entry checkboxes and **zip just the selected files/folders** — all with **no
  JavaScript**.
- **Access logging** — `--access-log PATH` writes one line per response in Common Log Format
  (`--access-log-format clf`/`combined`/`json`), separate from the diagnostic stderr log.
- **WebDAV** — `--dav` lets macOS Finder / Windows Explorer / Linux *mount* the share as a
  network drive (read-only); `--dav-write` adds write (PUT/DELETE/MKCOL/MOVE/COPY). Pure
  stdlib, same path-safety as everything else; writes honor `--auth`.
- **CORS, SPA fallback, cache control, security headers** — `--cors`, `--spa`, `--cache`,
  with `nosniff` everywhere and a scoped CSP on generated pages (off via `--no-security-headers`).
- **On-the-fly gzip** — text-like responses (and the directory listing) are gzipped when the
  client accepts it (RFC 9110: q-value negotiation, `Vary`, distinct ETag, ranges served
  identity). Already-compressed media is left alone (keeps `sendfile`). Off via `--no-compress`.
- **Frictionless LAN sharing** — `--qr` prints a scannable QR of the LAN URL (pure-stdlib QR
  encoder, with auto-detected LAN IP even on a `0.0.0.0` bind), and `--discoverable` advertises
  over mDNS/DNS-SD so the share shows up in Finder / file managers and at `<host>.local`.
- **HTTP/2** — `--http2` enables a *pure-stdlib* HTTP/2 server (ALPN `h2` over TLS, or h2c
  prior-knowledge). The HPACK and frame codecs are implemented against the RFCs with no
  third-party package.
- **HTTP/3** — optional, via `pip install servery[http3]` (the `aioquic` QUIC stack); the core
  stays zero-dependency.
- **Safe by default** — binds `127.0.0.1`, path-traversal + symlink-escape protection, a
  default socket timeout, and loud warnings when you expose it or run auth without TLS.
- **Free-threading ready** — runs under the no-GIL builds (3.13t/3.14t); immutable config,
  no module-level mutable state.

## Install

```bash
pip install servery            # core: zero dependencies
pip install servery[http3]     # optional HTTP/3 (aioquic)
```

Python 3.13+ (free-threaded builds supported).

## Library use

```python
from servery import Config, serve

serve(Config.create("./public", host="127.0.0.1", port=8000))
```

## A note on dependencies

The **core has zero third-party runtime dependencies** — enforced by a CI gate that fails the
build if the wheel ever declares one. HTTP/3 is the single, opt-in exception: it requires a real
QUIC + crypto stack the standard library does not provide, so it lives behind the
`servery[http3]` extra and never burdens the core. (For the curious, `servery._oscrypto` proves
the standard library *can* reach AEAD crypto with zero PyPI dependencies via `ctypes` → the OS
OpenSSL — the foundation a future native HTTP/3 could build on.)

It lives in the **file-server lane** (share a folder), not the web-framework lane — there is no
routing or app-building here. It is a dev / LAN / ad-hoc sharing tool, not a hardened
public-internet server.

## Documentation

📖 **[Full documentation site → mjbommar.github.io/servery](https://mjbommar.github.io/servery/)** —
[getting started](https://mjbommar.github.io/servery/getting-started/), task-oriented
[guides](https://mjbommar.github.io/servery/guide/serving/) with examples,
[recipes](https://mjbommar.github.io/servery/recipes/),
[how to extend it](https://mjbommar.github.io/servery/extending/library/), and a full
[CLI reference](https://mjbommar.github.io/servery/reference/cli/).

The deep-dive design docs (also rendered on the site):

| Document | What it covers |
|---|---|
| [docs/VISION.md](docs/VISION.md) | The gap, positioning, target users, non-goals |
| [docs/PRINCIPLES.md](docs/PRINCIPLES.md) | Zero-dependency mandate and the scope rubric |
| [docs/TRANSPORTS.md](docs/TRANSPORTS.md) | The tiered HTTP/1.1→2→3 transport model and crypto policy |
| [docs/STANDARDS.md](docs/STANDARDS.md) | RFC 9110/9111/9112 compliance map (MUST/SHOULD + tests) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module layout, request lifecycle, security design |
| [docs/BEST-PRACTICES.md](docs/BEST-PRACTICES.md) | 2026 stdlib-only implementation best practices |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Testable requirements and the CLI surface |
| [docs/ROADMAP.md](docs/ROADMAP.md) | How it was built, milestone by milestone |
| [docs/DYNAMIC.md](docs/DYNAMIC.md) | Phased roadmap for opt-in CGI / WSGI / ASGI (stdlib-only) |
| [docs/REFERENCES.md](docs/REFERENCES.md) | Prior art and stdlib feasibility map |

## Development

Requires [uv](https://docs.astral.sh/uv/). All gates run locally exactly as in CI:

```bash
uv sync                      # dev tooling (never ships in the wheel)
uv run pre-commit install    # local commit gates
make check                   # lint (ruff) + type (ty) + security (bandit) + tests
make build                   # build + zero-dependency gate
```

CI runs the suite on Linux/macOS/Windows × CPython 3.13/3.14, the free-threaded 3.13t/3.14t
builds, and 3.15. See [CONTRIBUTING.md](CONTRIBUTING.md).

A reproducible per-transport benchmark suite (pytest-benchmark) lives in `benchmarks/`:

```bash
uv run --group bench pytest benchmarks/   # HTTP/1.1, TLS, HTTP/2, WSGI, CGI, ASGI, proxy, …
```

See [BENCHMARKS.md](BENCHMARKS.md) for reference numbers, the HTTP/3 case, and the
regression-comparison workflow.

## License

MIT

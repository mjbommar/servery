# servery

[![CI](https://github.com/mjbommar/servery/actions/workflows/ci.yml/badge.svg)](https://github.com/mjbommar/servery/actions/workflows/ci.yml)
[![Security](https://github.com/mjbommar/servery/actions/workflows/security.yml/badge.svg)](https://github.com/mjbommar/servery/actions/workflows/security.yml)
![Python](https://img.shields.io/badge/python-3.13%2B-blue)
![Dependencies](https://img.shields.io/badge/runtime%20dependencies-zero-brightgreen)

A **zero-dependency, pure-Python** HTTP file server — *a batteries-included `python -m http.server`*.

Serve or share a directory over HTTP with the niceties people expect from tools like
[miniserve](https://github.com/svenstaro/miniserve) or `npx serve` — rich sortable
directory listings, file upload, HTTP Basic Auth, HTTPS, range/resumable downloads — while
depending on **nothing but the Python standard library**, forever.

```console
$ servery                 # serve the current directory on http://127.0.0.1:8000
$ python -m servery ./pub --port 9000
```

> **Status: design phase.** No implementation yet — the repository currently contains the
> requirements, architecture, and roadmap that the build will follow. See the docs below.

## Why

The stdlib `http.server` gives you a bare, unsortable listing and nothing else — no auth,
no upload, no HTTPS without hand-rolling. The polished alternatives are either not Python
(miniserve is Rust; `serve`/`http-server` are Node) or not zero-dependency (`updog` needs
Flask). The pure-stdlib options that exist (`uploadserver`, `tiny-http-server`) inherit
http.server's plain listing. **No existing zero-dependency, pure-Python tool combines a rich
sortable listing + basic auth + upload + HTTPS.** servery fills exactly that gap.

It lives in the **file-server lane** (share a folder), *not* the web-framework lane — there
is no routing or app-building here.

## Design at a glance

- **Zero third-party dependencies** — pure Python standard library only. Non-negotiable.
- **Python 3.13+** — lands us natively after `cgi` was removed, so upload uses one clean
  hand-rolled multipart parser, no legacy branch.
- **Standards-compliant** — a conformant HTTP/1.1 origin server (RFC 9110/9111/9112):
  persistent connections, `Range`/`206`, the full conditional-request ladder with weak
  `ETag`s, and `Cache-Control`. (HTTP/2/3 are out — no stdlib HPACK/QPACK/QUIC.)
- **Secure by default** — binds `127.0.0.1`, protects against path traversal and symlink
  escape, constant-time auth comparison, loud warning if Basic Auth runs without TLS,
  and secure web-facing headers on by default (`X-Content-Type-Options: nosniff`,
  scoped CSP on generated pages, HSTS under TLS) with strict listing escaping.
- **Honest scope** — a dev / LAN / ad-hoc sharing tool, not a production-hardened server.

## Documentation

| Document | What it covers |
|---|---|
| [docs/VISION.md](docs/VISION.md) | The gap, positioning, target users, non-goals |
| [docs/PRINCIPLES.md](docs/PRINCIPLES.md) | Zero-dependency mandate and the scope rubric |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Testable requirements, full CLI surface, decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module layout, request lifecycle, security design |
| [docs/STANDARDS.md](docs/STANDARDS.md) | RFC 9110/9111/9112 compliance map (per-feature MUST/SHOULD + tests) |
| [docs/BEST-PRACTICES.md](docs/BEST-PRACTICES.md) | 2026 stdlib-only implementation best practices (zero-dep) |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased milestones from v0.1 to v1.0 |
| [docs/REFERENCES.md](docs/REFERENCES.md) | Prior art, what to borrow, stdlib feasibility map |

## Development

Requires [uv](https://docs.astral.sh/uv/). All gates run locally exactly as in CI:

```bash
uv sync                      # dev tooling (never ships in the wheel)
uv run pre-commit install    # local commit gates
make check                   # lint + type + security + tests
make build                   # build + zero-dependency gate
```

The runtime stays **zero-dependency** — a CI gate fails the build if the wheel
ever declares a dependency. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT

# servery

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
- **Safe by default** — binds `127.0.0.1`, protects against path traversal and symlink
  escape, constant-time auth comparison, loud warning if Basic Auth runs without TLS.
- **Honest scope** — a dev / LAN / ad-hoc sharing tool, not a production-hardened server.

## Documentation

| Document | What it covers |
|---|---|
| [docs/VISION.md](docs/VISION.md) | The gap, positioning, target users, non-goals |
| [docs/PRINCIPLES.md](docs/PRINCIPLES.md) | Zero-dependency mandate and the scope rubric |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Testable requirements, full CLI surface, decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module layout, request lifecycle, security design |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased milestones from v0.1 to v1.0 |
| [docs/REFERENCES.md](docs/REFERENCES.md) | Prior art, what to borrow, stdlib feasibility map |

## License

MIT

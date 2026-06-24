# servery

A **zero-dependency, pure-Python** HTTP file server — *a batteries-included
`python -m http.server`*.

Serve or share a directory over HTTP with the niceties people expect from tools like
[miniserve](https://github.com/svenstaro/miniserve) or `npx serve` — rich sortable
directory listings, uploads, HTTP Basic Auth, HTTPS (including **automatic Let's
Encrypt certificates**), range/resumable downloads, on-the-fly archives, **WebDAV**,
even **HTTP/2** — while the core depends on **nothing but the Python standard
library**.

## Run it right now — no install

```bash
# one self-contained file, straight from a pipe (latest release):
curl -fsSL https://github.com/mjbommar/servery/releases/latest/download/servery.py | python3 - ./public -p 8000

# …or from PyPI with uv:
uvx servery ./public --port 8000
```

The piped `servery.py` is the released package amalgamated into one auditable file
(pure stdlib). Inspect it first if you like (`curl -fsSL <url> | less`), pin a version
(`…/releases/download/v1.3.0/servery.py`), or grab the `servery.pyz` zipapp.

```console
$ servery                                  # serve the current directory on http://127.0.0.1:8000
$ servery ./public --port 9000
$ servery --upload --auth me:secret        # password-protected drop box
$ servery --acme example.com               # automatic, browser-trusted HTTPS
$ servery --dav --dav-write --auth me:s3cret  # mount it as a network drive
```

## Why servery?

<div class="grid cards" markdown>

-   :material-package-variant-closed:{ .lg .middle } __Zero dependencies__

    ---

    The core installs nothing from PyPI — enforced by a CI gate. One `pip install`,
    or no install at all via the single-file bundle.

    [:octicons-arrow-right-24: The principles](PRINCIPLES.md)

-   :material-rocket-launch:{ .lg .middle } __Batteries included__

    ---

    Listings, uploads, auth, HTTPS, ACME, WebDAV, gzip, ranges, archives, CORS, SPA,
    HTTP/2 — the things `python -m http.server` makes you go without.

    [:octicons-arrow-right-24: Getting started](getting-started.md)

-   :material-lock-check:{ .lg .middle } __Safe by default__

    ---

    Binds loopback, blocks path traversal and symlink escapes, defaults a socket
    timeout, and shouts when you expose it without TLS.

    [:octicons-arrow-right-24: Architecture & security](ARCHITECTURE.md)

-   :material-language-python:{ .lg .middle } __Free-threading ready__

    ---

    Runs under the no-GIL CPython builds (3.13t/3.14t): immutable config, no
    module-level mutable state.

    [:octicons-arrow-right-24: Transports](TRANSPORTS.md)

</div>

## Install

=== "pip"

    ```bash
    pip install servery          # core: zero dependencies
    pip install servery[http3]   # optional HTTP/3 (aioquic)
    ```

=== "uv"

    ```bash
    uv tool install servery      # or run ad-hoc with: uvx servery
    ```

=== "single file"

    ```bash
    curl -fsSL https://github.com/mjbommar/servery/releases/latest/download/servery.py -o servery.py
    python3 servery.py ./public
    ```

Python 3.13+ (free-threaded builds supported).

## Where to next

- **New here?** Start with [Getting started](getting-started.md).
- **Want the philosophy?** Read the [Vision & goals](VISION.md).
- **Building on it?** servery is also a small library — `from servery import Config, serve`.

!!! note "It lives in the file-server lane"

    servery shares a folder over HTTP — it is **not** a web framework. There is no
    routing or app-building in the core (though you can mount a WSGI/ASGI/CGI app).
    It is a dev / LAN / ad-hoc sharing tool, not a hardened public-internet server.

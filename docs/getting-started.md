# Getting started

## Install

=== "pip"

    ```bash
    pip install servery
    ```

=== "uv"

    ```bash
    uv tool install servery       # installs the `servery` command
    # or run it ad-hoc without installing:
    uvx servery
    ```

=== "no install (single file)"

    ```bash
    curl -fsSL https://github.com/mjbommar/servery/releases/latest/download/servery.py -o servery.py
    python3 servery.py
    ```

servery needs **Python 3.13+** (the free-threaded 3.13t/3.14t builds work too) and
has **no third-party runtime dependencies**.

## Your first server

Run `servery` in any directory:

```console
$ servery
servery: serving /home/you/project on http://127.0.0.1:8000/
```

Open <http://127.0.0.1:8000/> and you get a rich, sortable directory listing —
sizes, modified times, a search box, per-type icons, and a light/dark theme — all
rendered server-side with **no JavaScript**.

By default servery binds **loopback only** (`127.0.0.1`), so nothing is reachable
from the network until you ask for it. Press ++ctrl+c++ to stop.

## Serve a specific folder, on a specific port

```bash
servery ./public --port 9000
```

- `directory` (positional) — what to serve; defaults to the current directory.
- `-p, --port` — defaults to `8000`.
- `-b, --bind` — defaults to `127.0.0.1`. Use `0.0.0.0` to expose on your LAN.

## Share it on your network

```bash
servery --bind 0.0.0.0 --qr
```

`--bind 0.0.0.0` listens on every interface, and `--qr` prints a scannable QR code
of your LAN URL so a phone on the same Wi-Fi can open it instantly. servery prints a
warning when you expose it, because that's a deliberate, security-relevant choice:

```console
servery: serving /home/you/photos on http://0.0.0.0:8000/
servery: WARNING bound to 0.0.0.0 — reachable from the network
```

See [Sharing on a LAN](guide/lan.md) for `--discoverable` (mDNS/Bonjour) and QR
details.

## A password-protected drop box

Accept uploads, protected by a password:

```bash
servery --upload --auth me:secret
```

Now `POST`ing files (or using the upload form in the listing) writes them into the
served folder, but only with the right credentials. Auth is meaningless without
encryption, so pair it with HTTPS for anything real:

```bash
servery --upload --auth me:secret --tls-self-signed
```

!!! warning "Basic auth without TLS sends the password in the clear"

    servery warns you when `--auth` runs over plain HTTP. Use `--tls-self-signed`
    (instant, ad-hoc cert) on a LAN, or `--acme` for a real browser-trusted one.

## Automatic, browser-trusted HTTPS

If a domain points at your machine and port 80 is reachable, servery can fetch a
real Let's Encrypt certificate for you — with **zero extra dependencies**:

```bash
servery --acme example.com --acme-email you@example.com --acme-production
```

(It defaults to the Let's Encrypt **staging** CA so you can test safely; add
`--acme-production` for the real thing.) See [HTTPS & certificates](guide/https.md).

## Profiles: presets for common setups

`--profile` applies a bundle of flags so you don't have to remember them. Any
explicit flag still overrides the preset.

```bash
servery --profile share     # bind LAN + self-signed TLS, ready to share
servery --profile inbox     # LAN + TLS + uploads: a secure drop box
servery --profile cdn       # long cache + CORS, for serving static assets
```

The full list: `app`, `cdn`, `dev`, `inbox`, `local`, `public-readonly`,
`public-readwrite`, `share` — run `servery --help` to see them, or browse the
[Guide](guide/serving.md) for each feature they bundle.

## Next steps

- **[Vision & goals](VISION.md)** — what servery is (and isn't), and why.
- **[Architecture](ARCHITECTURE.md)** — how a request flows through it, and the
  security model.
- **[Transports](TRANSPORTS.md)** — the HTTP/1.1 → HTTP/2 → HTTP/3 tiering.

Then dig into the task-oriented **[Guide](guide/serving.md)** — [uploads &
auth](guide/uploads.md), [HTTPS & certificates](guide/https.md), [WebDAV
mounts](guide/webdav.md), [compression & caching](guide/web.md),
[HTTP/2 & HTTP/3](guide/protocols.md) — each with copy-paste examples. Every flag is
also documented by `servery --help`.

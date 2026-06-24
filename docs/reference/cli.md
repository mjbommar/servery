# CLI reference

The complete `servery` command-line surface. Every flag maps to a
[`Config.create()`](../extending/library.md) keyword if you're using servery as a
library.

```text
servery [OPTIONS] [directory]
```

`directory` — the folder to serve (default: the current directory).

Run `servery --help` for the same list inline, or `servery --version`.

## Basics

| Flag | Default | Description |
| --- | --- | --- |
| `-p, --port PORT` | `8000` | port to listen on (if taken, the next free port is used) |
| `-b, --bind ADDR` | `127.0.0.1` | bind address (`0.0.0.0` to expose on the network) |
| `--show-hidden` | off | include dotfiles in listings |
| `-q, --quiet` | off | suppress request logging and the startup banner |
| `--timeout SECONDS` | `30` | per-connection socket timeout |
| `--profile NAME` | — | apply a [preset bundle](#profiles) of flags |

## Uploads

| Flag | Default | Description |
| --- | --- | --- |
| `--upload` | off | accept `POST multipart/form-data` uploads into the tree |
| `--max-upload-size BYTES` | 100 MiB | maximum accepted upload size |
| `--allow-overwrite` | off | let uploads overwrite existing files |
| `--upload-extract` | off | safely expand uploaded zip/tar archives (requires `--upload`) |

→ [Uploads & authentication](../guide/uploads.md)

## Authentication

| Flag | Default | Description |
| --- | --- | --- |
| `--auth USER:PASS` | off | require HTTP Basic auth (or `USER:sha256:HEX` / `USER:sha512:HEX`) |

## HTTPS & certificates

| Flag | Default | Description |
| --- | --- | --- |
| `--tls-cert PATH` | — | TLS certificate chain (PEM); enables HTTPS |
| `--tls-key PATH` | — | TLS private key (PEM) |
| `--tls-self-signed` | off | generate an ad-hoc self-signed cert at startup |
| `--tls-password-file PATH` | — | file holding the private-key passphrase |
| `--tls-help` | — | print how to generate a self-signed cert, then exit |
| `--acme DOMAIN` | — | obtain a Let's Encrypt cert for DOMAIN via ACME HTTP-01 (repeatable) |
| `--acme-email EMAIL` | — | ACME account contact email |
| `--acme-production` | **staging** | use the production Let's Encrypt CA |

→ [HTTPS & certificates](../guide/https.md)

## WebDAV

| Flag | Default | Description |
| --- | --- | --- |
| `--dav` | off | enable a (read-only) WebDAV endpoint, mountable as a drive |
| `--dav-write` | off | allow WebDAV writes (requires `--dav`; use with `--auth`) |

→ [WebDAV](../guide/webdav.md)

## LAN sharing

| Flag | Default | Description |
| --- | --- | --- |
| `--qr` | off | print a scannable QR code of the LAN URL on startup |
| `--discoverable` | off | advertise over mDNS/DNS-SD (Bonjour) |

→ [Sharing on a LAN](../guide/lan.md)

## Web behaviors

| Flag | Default | Description |
| --- | --- | --- |
| `--cache SECONDS` | `no-cache` | `Cache-Control: max-age` for file responses |
| `--cors` | off | send permissive CORS headers (`Access-Control-Allow-Origin: *`) |
| `--spa` | off | serve `/index.html` for unknown paths (single-page apps) |
| `--no-compress` | (gzip on) | disable on-the-fly gzip of text-like responses |
| `--no-security-headers` | (on) | disable servery's default security response headers |
| `--access-log PATH` | — | write an access log to PATH |
| `--access-log-format {clf,combined,json}` | `clf` | access log format |

→ [Compression, caching & headers](../guide/web.md)

## Protocols & concurrency

| Flag | Default | Description |
| --- | --- | --- |
| `--http2` | off | HTTP/2 (ALPN `h2` over TLS, or `h2c` cleartext) |
| `--http3` | off | HTTP/3 over QUIC (needs TLS + `servery[http3]`) |
| `--max-workers N` | unbounded | bound concurrency to N worker threads |

→ [HTTP/2, HTTP/3 & concurrency](../guide/protocols.md)

## Apps & proxying (opt-in; replace file serving)

| Flag | Description |
| --- | --- |
| `--wsgi MODULE:APP` | serve a WSGI application |
| `--asgi MODULE:APP` | serve an ASGI application (experimental) |
| `--cgi DIR` | execute CGI scripts from DIR (runs code; off by default) |
| `--proxy PREFIX=URL` | reverse-proxy `PREFIX…` to an upstream (repeatable; composes with file serving) |

→ [Running apps & proxying](../extending/apps.md)

## Profiles

`--profile NAME` applies a preset bundle of flags. **Any explicit flag still
overrides the preset.** Profiles that expose a writable surface to the network
require `--auth`.

| Profile | Bundles |
| --- | --- |
| `local` | the safe default — `127.0.0.1`, read-only, cleartext |
| `share` | bind `0.0.0.0` + self-signed TLS |
| `inbox` | bind `0.0.0.0` + TLS + `--upload` *(requires `--auth`)* |
| `public-readonly` | bind `0.0.0.0` + TLS + 1-hour cache |
| `public-readwrite` | bind `0.0.0.0` + TLS + `--upload` *(requires `--auth`)* |
| `cdn` | bind `0.0.0.0` + TLS + 1-year cache + CORS + HTTP/2 |
| `dev` | `127.0.0.1` + SPA fallback + CORS |
| `app` | bind `0.0.0.0` + TLS + `--max-workers` = CPU count |

```bash
servery --profile cdn ./assets        # long-cache static origin
servery --profile inbox --auth me:s3  # secure LAN drop box
```

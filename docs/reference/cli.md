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
| `--upload` | off | accept `POST multipart/form-data` uploads, and **resumable `Content-Range` `PUT`** uploads, into the tree |
| `--max-upload-size BYTES` | 100 MiB | maximum accepted upload size |
| `--allow-overwrite` | off | let uploads overwrite existing files |
| `--write-lock-timeout SECONDS` | `0` | wait this long for another in-process write to the same canonical target; `0` rejects immediately |
| `--partial-upload-ttl SECONDS` | `86400` | discard a resumable sidecar older than this before its next locked operation; `0` disables expiry |
| `--max-partial-uploads COUNT` | `128` | maximum outstanding resumable sidecars; `0` disables the count budget |
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
| `--dav-lock-mode {class1,compat,enforced}` | `enforced` | writable-DAV lock policy; read-only DAV always advertises honest class 1 |
| `--max-propfind-entries N` | `10000` | maximum children in `PROPFIND Depth: 1`; over-limit requests receive `507` |

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
| `--no-compress` | (on) | disable on-the-fly compression (zstd on 3.14+, else gzip) of text-like responses |
| `--max-compress-size BYTES` | 10 MiB | largest static file compressed in memory; `0` disables static-file compression |
| `--compression-cache-size BYTES` | `0` | byte budget for encoded static representations; `0` disables the cache |
| `--max-buffered-response BYTES` | 1 MiB | largest HTTP/2/3 file kept on the buffered fast path; larger files stream, and `0` streams every nonempty file |
| `--max-listing-entries N` | `100000` | maximum entries considered by an HTML directory listing |
| `--listing-page-size N` | `1000` | rows rendered per listing page |
| `--listing-details-threshold N` | `10000` | above this count, omit expensive listing metadata, metrics, and timeline |
| `--no-security-headers` | (on) | disable servery's default security response headers |
| `--access-log PATH` | — | write an access log to PATH |
| `--access-log-format {clf,combined,json}` | `clf` | access log format |

→ [Compression, caching & headers](../guide/web.md)

## Protocols & concurrency

| Flag | Default | Description |
| --- | --- | --- |
| `--http2` | off | HTTP/2 (ALPN `h2` over TLS, or `h2c` cleartext) |
| `--max-h2-streams N` | `100` | advertised and enforced active-stream limit per HTTP/2 connection |
| `--http3` | off | HTTP/3 over QUIC alongside TCP fallback (needs TLS + `servery[http3]`) |
| `--http3-only` | off | expert mode with no TCP fallback; requires `--http3` |
| `--http3-port PORT` | TCP port | UDP listener/advertised port; use `0` for an ephemeral test port |
| `--max-connections N` | `256` | simultaneous HTTP/TLS/ASGI connections or HTTP/3 sessions |
| `--max-workers N` | unbounded | bound concurrency to N worker threads |
| `--max-request-body BYTES` | 100 MiB | accepted app/proxy/WebDAV request body (separate from file-upload size) |
| `--keepalive-drain-limit BYTES` | 64 KiB | drain at most this much unread accepted body to reuse a connection; otherwise close |

→ [HTTP/2, HTTP/3 & concurrency](../guide/protocols.md)

## TFTP (opt-in; separate UDP listener)

Serves the **same directory** over TFTP (RFC 1350) alongside HTTP — for PXE boot
and network gear. **No authentication or encryption**: trusted LAN / lab networks
only. Read-only unless `--tftp-write`.

| Flag | Default | Description |
| --- | --- | --- |
| `--tftp` | off | also serve the directory over TFTP on UDP (read-only) |
| `--tftp-port PORT` | `69` | UDP port for TFTP (below 1024 needs privileges) |
| `--tftp-write` | off | allow anonymous TFTP uploads (`WRQ`); requires `--tftp` |
| `--max-tftp-transfers N` | `32` | active TFTP transfer sockets/workers; saturation returns a busy error |

→ [HTTP/2, HTTP/3 & concurrency](../guide/protocols.md#tftp)

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
| `cdn` | bind `0.0.0.0` + TLS + 1-year cache + CORS + HTTP/2 + 32 MiB compression cache |
| `dev` | `127.0.0.1` + SPA fallback + CORS |
| `app` | bind `0.0.0.0` + TLS + `--max-workers` = CPU count |

```bash
servery --profile cdn ./assets        # long-cache static origin
servery --profile inbox --auth me:s3  # secure LAN drop box
```

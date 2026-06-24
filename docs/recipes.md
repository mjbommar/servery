# Recipes

Copy-paste solutions for common tasks. Each links to the relevant guide.

## Share this folder with the phone in my hand

```bash
servery --profile share --qr
```

Binds the LAN with a self-signed cert and prints a QR code to scan.
→ [Sharing on a LAN](guide/lan.md)

## Let someone drop files to me (securely)

```bash
servery --upload --auth me:secret --tls-self-signed --bind 0.0.0.0
# or the preset:
servery --profile inbox --auth me:secret --qr
```

→ [Uploads & authentication](guide/uploads.md)

## Mount the share as a drive in Finder/Explorer

```bash
# read-only:
servery --dav --bind 0.0.0.0
# read/write, with a password:
servery --dav --dav-write --auth me:secret --bind 0.0.0.0
```

→ [WebDAV](guide/webdav.md)

## Get a real, browser-trusted HTTPS certificate automatically

```bash
sudo servery --acme example.com --acme-email you@example.com \
     --acme-production --bind 0.0.0.0 --port 443
```

Needs the domain pointed at you and port 80 reachable for the challenge.
→ [HTTPS & certificates](guide/https.md)

## Serve a built front-end (SPA) with client-side routing

```bash
servery ./dist --spa
```

→ [Serving files](guide/serving.md#single-page-apps)

## Serve static assets as a CDN-style origin

```bash
servery ./assets --profile cdn        # long cache + CORS + HTTP/2 (TLS)
```

→ [Compression, caching & headers](guide/web.md)

## Download a whole folder in one click

Append to any directory URL:

```text
…/photos/?archive=zip
…/photos/?archive=tar.gz
```

Or tick a few files and hit **zip selected**.
→ [Serving files](guide/serving.md#download-a-whole-folder-as-an-archive)

## Run a local API behind the same origin as my files

```bash
servery --proxy /api=http://localhost:8001
```

→ [Running apps & proxying](extending/apps.md#reverse-proxy)

## Keep an access log for analytics

```bash
servery --access-log access.log --access-log-format combined
```

→ [Access logging](guide/web.md#access-logging-to-a-file)

## Use it in a test (ephemeral port, background thread)

```python
import threading
from servery import Config, make_server, server_url

server = make_server(Config.create("./fixtures", port=0))
threading.Thread(target=server.serve_forever, daemon=True).start()
base = server_url(server)   # http://127.0.0.1:<random>/
# … make requests against base …
server.shutdown()
```

→ [Using servery as a library](extending/library.md)

## Run it with no install at all

```bash
curl -fsSL https://github.com/mjbommar/servery/releases/latest/download/servery.py | python3 - ./public -p 8000
```

→ [Getting started](getting-started.md)

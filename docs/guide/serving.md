# Serving files

The default mode: point servery at a folder and it serves the files plus a rich,
JavaScript-free directory listing.

```bash
servery ./public --port 8000
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `directory` | current dir | what to serve |
| `-p, --port` | `8000` | port to listen on |
| `-b, --bind` | `127.0.0.1` | bind address (`0.0.0.0` to expose on the LAN) |
| `--show-hidden` | off | include dotfiles in listings |
| `-q, --quiet` | off | suppress the request log and startup banner |

## The directory listing

Browsing to a folder renders a listing with sizes, modified times, directories
first, per-type icons, relative timestamps, an aggregate metrics strip, and a
light/dark/auto theme — entirely **server-side, no JavaScript**.

Several views are driven by query parameters (so they're shareable links):

| Query | Effect |
| --- | --- |
| `?C=N&O=A` | sort by **N**ame / **S**ize / **M**time, **A**sc / **D**esc (Apache convention) |
| `?q=report` | filter the listing to names containing `report` |
| `?ext=pdf` | filter to a file-type facet |
| `?page=2` | paginate large directories |

If a folder contains an `index.html` (or `index.htm`), servery serves that instead
of the listing — the same as a normal web server.

## Downloading

- Click any file to view it inline (correct `Content-Type`).
- The **↓** affordance on each row, or appending **`?download=1`**, forces a
  *Save as…* dialog (`Content-Disposition: attachment`).
- Downloads support **HTTP `Range`** — large files resume, and media seeks work —
  with strong `ETag`s and the full conditional-request ladder
  (`If-None-Match` / `If-Modified-Since` → `304`). Transfers use zero-copy
  `sendfile` where possible.

## Download a whole folder as an archive

Append `?archive=` to any directory URL to stream it as one archive (built on the
fly, never buffered to disk):

```text
http://localhost:8000/photos/?archive=tar.gz
http://localhost:8000/photos/?archive=zip
```

Only regular files are included — symlinks are skipped, so an archive can never leak
content from outside the served tree.

## Pick a few files → one zip (no JavaScript)

Every listing row has a checkbox and the footer has a **zip selected** button. Tick
the files (and/or folders) you want and download just those as a single zip. Under
the hood that's a plain HTML form — `?sel=a.txt&sel=b.txt` — so it works with
JavaScript disabled. Selected names are validated as direct children, so a crafted
`sel` can't escape the directory.

## Single-page apps

For a client-side-routed app, serve `index.html` for unknown paths:

```bash
servery ./dist --spa
```

A request for `/some/route` that doesn't map to a real file falls back to
`/index.html`, letting the app's router take over.

## See also

- [Compression, caching & headers](web.md) — gzip, `Cache-Control`, CORS, security
  headers.
- [HTTP/2 & HTTP/3](protocols.md) — modern transports.
- [Sharing on a LAN](lan.md) — expose it to other devices, with a QR code.

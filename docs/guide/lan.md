# Sharing on a LAN

The "run it, scan it, you're in" path: serve a folder to the phones and laptops on
your network with the least possible friction.

## Expose it

By default servery binds loopback only. To reach it from other devices, bind all
interfaces:

```bash
servery --bind 0.0.0.0
```

servery prints a warning when you do this, because it's a real security decision —
anyone on the network can now reach the share. Pair it with [`--auth`](uploads.md)
and [TLS](https.md) if the contents aren't meant to be public.

## Scan to open: `--qr`

```bash
servery --bind 0.0.0.0 --qr
```

`--qr` prints a scannable **QR code** of the server's LAN URL on startup — point a
phone camera at the terminal and it opens. The QR encoder is **pure stdlib** (no
dependency), and the LAN IP is auto-detected even when you bind `0.0.0.0`.

```console
servery: serving /home/you/share on http://0.0.0.0:8000/
servery: scan to open on another device → http://192.168.1.42:8000/

  █▀▀▀▀▀█ ▀ ▄▀█ █▀▀▀▀▀█
  █ ███ █ █▄▀▀▄ █ ███ █
  █ ▀▀▀ █ ▀█ ▄█ █ ▀▀▀ █
  ▀▀▀▀▀▀▀ █▄▀▄▀ ▀▀▀▀▀▀▀
  …
```

## Show up in network browsers: `--discoverable`

```bash
servery --bind 0.0.0.0 --discoverable
```

`--discoverable` advertises the server over **mDNS / DNS-SD** (Bonjour,
`_http._tcp.local`) so it appears in macOS Finder's network view, Linux file
managers, and other Bonjour browsers — and resolves at `http://<hostname>.local`.

## The one-liner

The `share` profile bundles a LAN bind with a self-signed certificate, ready to go:

```bash
servery --profile share --qr
```

For an **upload** drop box on the LAN, use `inbox`:

```bash
servery --profile inbox --qr      # LAN + TLS + uploads
```

## Mount it as a drive instead

If you'd rather mount the share in the OS file manager (read or read/write) than use
a browser, servery also speaks WebDAV — see [WebDAV](webdav.md).

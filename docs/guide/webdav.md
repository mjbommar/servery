# WebDAV — mount the share as a drive

With `--dav`, servery exposes a WebDAV endpoint (RFC 4918) that macOS Finder,
Windows Explorer, and Linux file managers can **mount as a network drive** — browse
it like a local folder, no browser needed. It's pure stdlib (`xml.etree`) and reuses
servery's path-safety, atomic writes, and ETags.

## Read-only mount

```bash
servery --dav --bind 0.0.0.0
```

This advertises WebDAV (with the compliance class clients expect to mount
read-write), answers `PROPFIND`/`OPTIONS`, and serves files — but **rejects all
writes**. Good for letting people mount-and-browse safely.

## Read/write mount

```bash
servery --dav --dav-write --auth me:secret --bind 0.0.0.0
```

`--dav-write` enables the write methods (`PUT`, `DELETE`, `MKCOL`, `MOVE`, `COPY`,
`PROPPATCH`) so the mounted drive is writable. Because that lets clients create,
move, and delete files, it's **off by default**, honors `--auth`, respects
`--allow-overwrite`, and prints a startup warning if you enable it without auth.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--dav` | off | enable a read-only WebDAV endpoint (mountable) |
| `--dav-write` | off | enable WebDAV writes (requires `--dav`; use with `--auth`) |

## Mounting it

=== "macOS (Finder)"

    **Go → Connect to Server…** (++cmd+k++), then enter:

    ```text
    http://192.168.1.42:8000/
    ```

=== "Windows (Explorer)"

    **This PC → Map network drive…**, then:

    ```text
    http://192.168.1.42:8000/
    ```

    (Windows' built-in WebDAV client has its own restrictions on Basic auth over
    plain HTTP and a file-size cap — both are client-side registry settings.)

=== "Linux"

    ```bash
    gio mount dav://192.168.1.42:8000/
    # or with davfs2:
    sudo mount -t davfs http://192.168.1.42:8000/ /mnt/share
    ```

## Safety notes

- The `Destination` header on `MOVE`/`COPY` goes through the **same containment
  check** as every other path, so a crafted destination can't escape the served
  root.
- Destructive methods are gated behind `--dav-write`; a plain `--dav` share executes
  no writes.
- servery advertises a "class 2" lock with a stub lock token (the industry norm for
  minimal servers) so Finder/Explorer will mount read-write; it does not maintain
  real lock state.

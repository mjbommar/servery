# WebDAV

With `--dav`, servery exposes a WebDAV endpoint (RFC 4918) that macOS Finder,
Windows Explorer, and Linux file managers can **mount as a network drive** — browse
it like a local folder, no browser needed. It's pure stdlib (`xml.etree`) and reuses
servery's path-safety, atomic writes, and ETags.

## Read-only mount

```bash
servery --dav --bind 0.0.0.0
```

This honestly advertises WebDAV compliance class 1, answers `PROPFIND`/`OPTIONS`,
and serves files — but **rejects all writes and locking methods**. Good for letting
people mount-and-browse safely.

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
| `--dav-lock-mode` | `enforced` | `class1`, `compat`, or real in-memory `enforced` locking for writable DAV |
| `--max-propfind-entries` | `10000` | maximum Depth-1 children; excess returns `507` rather than a partial result |

## Lock policy

Writable DAV defaults to real exclusive, depth-infinity locks. servery stores each
token, root, owner, and expiry in memory; refreshes a submitted token; exposes live
`lockdiscovery`; and requires the token for affected `PUT`, `DELETE`, `MKCOL`,
`MOVE`, `COPY`, and `PROPPATCH` operations. Locks also protect descendants and stop
a parent operation from deleting a locked child.

Choose the policy explicitly when client compatibility calls for it:

| Mode | DAV claim | Behavior |
| --- | --- | --- |
| `enforced` | class 2 | real single-process mutual exclusion (writable default) |
| `class1` | class 1 | no `LOCK`/`UNLOCK`; protocol-honest and simplest |
| `compat` | class 2 | returns a token but does not enforce it; emits a startup warning |

Locks vanish on restart and do not coordinate with unrelated local processes or a
second servery process. The per-target write coordinator likewise protects writes
inside one server process; same-directory temporary files and atomic replacement
ensure clients never observe a partially committed file.

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
- `PROPFIND Depth: 1` never returns a silently incomplete `207`: collections above
  `--max-propfind-entries` receive explicit `507 Insufficient Storage`.
- `compat` mode exists for clients that refuse class 1 but is deliberately named and
  warned as non-enforcing; use `enforced` wherever clients support normal tokens.

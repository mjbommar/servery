# Design: WebDAV read/write — `--dav` / `--dav-write`

Status: implemented, including explicit lock modes and bounded Depth-1 discovery.
RFC 4918. Pure stdlib (`xml.etree.ElementTree`, `shutil`, `tempfile`, `uuid`,
`urllib.parse`). Goal: mount the share in macOS Finder, Windows Explorer, and Linux
gio/davfs2 without overstating the guarantees servery provides.

## Method set

OPTIONS, PROPFIND, GET/HEAD, PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK, and
PROPPATCH.

- **OPTIONS** advertises class 1 for read-only or `dav_lock_mode=class1`, and class
  2 only for writable `compat`/`enforced` modes. `MS-Author-Via: DAV` and `Allow`
  agree with the selected surface.
- **PROPFIND** accepts Depth 0/1; infinity returns `403 propfind-finite-depth`.
  Depth 1 scans at most `max_propfind_entries` children (10,000 by default) and
  returns explicit `507` rather than a silently partial `207`. Live properties
  include resource type, size, dates, MIME type, ETag, display name, supported lock,
  and real lock discovery where applicable.
- **PUT** streams to a same-directory temporary file and atomically replaces the
  target, bounded by `max_upload_size`; it returns 201/204, 409 for a missing
  parent, 405 on a collection, and 412 when overwrite is disabled.
- **DELETE**, **MKCOL**, **MOVE**, and **COPY** use the shared per-canonical-target
  coordinator for the full check/write sequence. MOVE/COPY validate `Destination`
  through the same containment choke point as GET and honor `Overwrite: F`.
- **PROPPATCH** accepts and discards dead-property changes, returning 207/200 for
  client interoperability without claiming persistence.

## Lock modes

`dav_lock_mode` is a resource/compatibility policy, not a hidden shortcut:

- **`enforced`** (writable default): an in-memory manager stores exclusive,
  depth-infinity root, token, owner, and expiry. It rejects overlapping locks,
  supports token refresh and UNLOCK, purges expired records, exposes
  `lockdiscovery`, and validates submitted `If`/`Lock-Token` values for affected
  writes. A child lock blocks destructive work on its parent.
- **`class1`**: does not advertise locking and returns 405 for LOCK/UNLOCK.
- **`compat`**: returns a fake opaque token without exclusion for clients that will
  not mount otherwise. Startup emits an explicit warning. Documentation never calls
  this mutual exclusion.

Read-only DAV always behaves as class 1; a server that cannot write has no useful
class-2 guarantee to advertise. Enforced locks vanish on restart and coordinate one
servery process only. Shared storage or multi-process lock persistence is out of
scope and is stated as such.

## Security and resource policy

- Every path uses the existing realpath containment model; a crafted Destination
  cannot escape through traversal or symlinks.
- Destructive methods require `--dav-write`; all methods honor `--auth`, and writable
  DAV without authentication produces a startup warning.
- `allow_overwrite` remains the content policy. `write_lock_timeout` selects
  immediate conflict (`0`, default) or bounded waiting; locking makes the choice
  reliable instead of changing it.
- Request bodies use the shared framing rules. Ambiguous length/transfer encoding is
  rejected, accepted bodies are capped, and unread early-error bodies force close.

The module is `servery/_webdav.py`; thin `do_*` methods in the HTTP/1 handler dispatch
to it. HTTP/2/3 deliberately remain read-only static transports.

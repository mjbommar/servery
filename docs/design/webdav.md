# Design: WebDAV read/write — `--dav` / `--dav-write`

Status: implementing. RFC 4918. Pure stdlib (`xml.etree.ElementTree`, `shutil`,
`tempfile`, `uuid`, `urllib.parse`). Goal: mount the share as a network drive in
macOS Finder, Windows Explorer, and Linux (gio/davfs2).

## Method set (minimal to mount read-write)
OPTIONS, PROPFIND, GET/HEAD (exist), PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK,
PROPPATCH.

- **OPTIONS** → `DAV: 1, 2` + `MS-Author-Via: DAV` + `Allow` (write verbs only when
  `--dav-write`). Class 2 (advertised, backed by a *stub* lock) is what makes Finder
  and Windows mount read-write.
- **PROPFIND** (Depth 0/1; `infinity` → 403 `propfind-finite-depth`) → `207
  Multi-Status`. Live props: `resourcetype`, `getcontentlength`, `getlastmodified`
  (RFC 1123 UTC), `creationdate` (ISO-8601), `getcontenttype`, `getetag`,
  `displayname`, static `supportedlock` + empty `lockdiscovery`.
- **PUT** atomic (temp + `os.replace`, bounded by `max_upload_size`) → 201/204; 409
  if parent missing; 405 on a collection; 412 if exists and not `--allow-overwrite`.
- **DELETE** (collections are always Depth-infinity, `shutil.rmtree`) → 204.
- **MKCOL** → 201; 405 exists; 409 parent missing; 415 if body.
- **MOVE/COPY**: `Destination` header (absolute URI) → **path validated through the
  same containment check as the Request-URI**; cross-host → 502. `Overwrite: F` +
  dest exists → 412. → 201/204.
- **LOCK/UNLOCK**: stub — accept, return an `opaquelocktoken:<uuid4>` + a
  `lockdiscovery` body, enforce nothing (the industry norm: dufs, Go memLS,
  Nextcloud FakeLocker). UNLOCK → 204.
- **PROPPATCH**: accept-and-discard → `207` with each prop `200 OK` (Windows/Office
  SET Win32 dead props and roll back the whole copy on any failure).

## Client gotchas (baked in)
- macOS probes `.DS_Store`/`._*`/`.metadata_never_index` — must 404 (never 403/500);
  every GET sends Content-Length (already true); `getlastmodified` in UTC.
- Windows needs `DAV: 1, 2`, `MS-Author-Via`, `displayname`, and PROPPATCH→207/200.
  (Basic-auth-over-HTTP + 50 MB limits are client registry settings — documented.)
- Linux gio/davfs2 are lenient (class 1 is enough; they don't require LOCK).

## Security (reuses servery's hardened primitives)
- The COPY/MOVE `Destination` path goes through the **same**
  `security.is_contained`/`translate_path` choke-point as GET — closes
  `Destination: …/../../etc` and symlink escape on the destination side.
- Destructive methods are gated behind `--dav-write` (off by default — a plain
  `--dav` share is read-only). All DAV methods honor `--auth`; enabling `--dav-write`
  without `--auth` adds a `startup_warnings()` entry. Overwrite respects
  `--allow-overwrite`. The stub lock stores no state (no DoS).

## Module / flags
- `servery/_webdav.py`: XML build/parse (one module-level `register_namespace("D",
  "DAV:")`, Clark-notation tags), Destination containment, per-method logic; thin
  `do_PROPFIND`/`do_PUT`/… on the handler dispatch to it when `config.dav`.
- Config: `dav: bool`, `dav_write: bool` (requires `dav`). Reuses `allow_overwrite`
  + `max_upload_size`. Read-only `--dav` still answers LOCK (stub) so clients mount.

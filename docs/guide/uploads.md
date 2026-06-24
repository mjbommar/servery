# Uploads & authentication

servery can be a **drop box**: a folder people upload into, optionally behind a
password.

## Accepting uploads

```bash
servery --upload
```

`--upload` adds an upload form to the directory listing and accepts
`POST multipart/form-data` into the served tree. Uploads are:

- **Streamed** to a temp file and committed with an atomic `os.replace` (no
  half-written files appear in the listing) — no buffering of the whole body in RAM.
- **Bounded** — `--max-upload-size BYTES` (default 100 MiB) rejects anything larger.
- **Non-destructive by default** — an upload that would overwrite an existing file is
  refused unless you pass `--allow-overwrite`.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--upload` | off | enable uploads |
| `--max-upload-size` | 100 MiB | maximum accepted body size |
| `--allow-overwrite` | off | let uploads replace existing files |
| `--upload-extract` | off | expand uploaded `zip`/`tar` archives in place |

### Auto-extracting archives

With `--upload-extract`, an uploaded `.zip`/`.tar(.gz)` is safely expanded into the
target directory — guarded against zip-slip (path traversal), zip-bombs, and
symlink escapes. Requires `--upload`.

```bash
servery --upload --upload-extract
```

## Requiring a password

```bash
servery --auth me:secret
```

`--auth USER:PASS` requires HTTP Basic auth for every request. To avoid putting a
plaintext password on the command line (or in shell history), pass a **pre-hashed**
credential:

```bash
# sha256:  printf 'secret' | sha256sum
servery --auth 'me:sha256:2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b'
```

Both `sha256:` and `sha512:` are accepted. Comparisons are constant-time, and both
the username and password are always compared (no early-out) so timing can't reveal
which half was wrong.

!!! warning "Basic auth needs TLS"

    Basic auth is base64, **not** encryption — over plain HTTP the credentials
    travel in the clear. servery prints a startup warning when `--auth` runs without
    TLS. On a LAN, add `--tls-self-signed`; for a real cert, see
    [HTTPS & certificates](https.md).

## A secure drop box

Put it together — uploads, a password, and an ad-hoc certificate:

```bash
servery --upload --auth me:secret --tls-self-signed --bind 0.0.0.0
```

Or use the preset that bundles exactly this:

```bash
servery --profile inbox          # LAN + self-signed TLS + uploads
```

For a *writable network drive* that Finder/Explorer can mount, see
[WebDAV](webdav.md).

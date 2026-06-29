"""The servery request handler.

We subclass the stdlib ``SimpleHTTPRequestHandler`` rather than reimplementing
HTTP: the base gives us correct request parsing, HEAD/GET dispatch, directory
redirects, and MIME typing. servery overrides what it improves:

* ``translate_path`` — routes every path through the :mod:`servery.security`
  containment check (closing the symlink-escape gap);
* ``list_directory`` — renders the rich, sortable, searchable listing;
* ``send_head`` / ``do_GET`` — add strong ``ETag``s, the conditional-request
  ladder (304), ``Range``/``206``/``416``, and zero-copy ``sendfile``;
* ``end_headers`` — injects ``X-Content-Type-Options: nosniff`` everywhere;
* ``protocol_version`` — HTTP/1.1 persistent connections.
"""

from __future__ import annotations

import contextlib
import http.cookies
import http.server
import io
import logging
import os
import shutil
import socket
import ssl
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, BinaryIO, ClassVar, cast, overload

from servery import (
    __version__,
    _compress,
    _conditional,
    _digest,
    _http1,
    _log,
    _resumable,
    archive,
    auth,
    listing,
    ranges,
    security,
    upload,
)

if TYPE_CHECKING:
    from _typeshed import SupportsRead, SupportsWrite

    from servery.server import ServeryHTTPServer

_COPY_BUFSIZE = 64 * 1024
# Zero-copy sendfile(2) only exists on Unix. On Windows ``socket.sendfile`` silently
# falls back to a pure-Python send loop, and because we set a socket timeout (see
# setup()), that loop runs a ``selector.select()`` before *every* 8 KiB send(2) — two
# syscalls per 8 KiB plus selector overhead. The result throttles plaintext downloads
# to a fraction of line rate (measured ~70 Mbps vs ~200+ for the userspace copy on the
# same host). Gate the fast path on real sendfile so Windows takes the copy below.
_HAS_SENDFILE = hasattr(os, "sendfile")
# Buffer for that userspace copy on a *plain* socket. wfile is unbuffered (wbufsize=0),
# so this is the send(2) size: a large buffer means ~256 sends for a 256 MiB file
# instead of tens of thousands, which matters on Windows. TLS keeps the smaller
# ``_COPY_BUFSIZE`` — OpenSSL re-chunks every write into ~16 KiB records regardless.
_RAW_COPY_BUFSIZE = 1024 * 1024
# CSP for servery-GENERATED pages (listing / error): no scripts, inline styles
# only, self forms. Served files are NOT given a CSP (it would break real sites).
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'self'"
)

# On-brand error page, replacing the bland stdlib default — same design language as
# the directory listing (system font, OS light/dark, the listing's accent). A
# `%`-format template (no literal `%`): the base class fills code/message/explain,
# all already HTML-escaped by `http.server`.
_ERROR_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(code)d %(message)s \N{MIDDLE DOT} servery</title>
<style>
:root { color-scheme: light dark; --accent: #2563eb; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh;
  display: grid; place-items: center; padding: 2rem; background: Canvas; color: CanvasText; }
main { max-width: 32rem; text-align: center; }
.code { font-size: 4.5rem; font-weight: 700; line-height: 1;
  letter-spacing: -0.03em; opacity: 0.85; }
.msg { font-size: 1.3rem; font-weight: 600; margin: 0.5rem 0 0; }
.explain { opacity: 0.7; margin: 0.5rem 0 1.75rem; }
a.home { color: var(--accent); text-decoration: none; font-weight: 500; }
a.home:hover { text-decoration: underline; }
footer { margin-top: 2.5rem; font-size: 0.8rem; opacity: 0.5; }
</style>
</head>
<body>
<main>
<div class="code">%(code)d</div>
<p class="msg">%(message)s</p>
<p class="explain">%(explain)s</p>
<a class="home" href="/">\N{LEFTWARDS ARROW} Back to home</a>
<footer>served by servery</footer>
</main>
</body>
</html>
"""


def _copy_n(
    source: SupportsRead[bytes],
    dest: SupportsWrite[bytes],
    count: int,
    bufsize: int = _COPY_BUFSIZE,
) -> None:
    """Copy exactly ``count`` bytes (or until EOF) from ``source`` to ``dest``."""
    remaining = count
    while remaining > 0:
        chunk = source.read(min(bufsize, remaining))
        if not chunk:
            break
        dest.write(chunk)
        remaining -= len(chunk)


def _content_disposition(filename: str) -> str:
    """Build a Content-Disposition with an ASCII fallback + UTF-8 (RFC 6266/8187)."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    # Drop control characters (incl. CR/LF) so a filesystem-derived name can never
    # inject a response header. The filename* form is percent-encoded already.
    ascii_name = "".join(c for c in ascii_name if c.isprintable()).replace('"', "")
    extended = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{extended}"


class _ChunkedWriter:
    """Wrap ``wfile`` to emit HTTP/1.1 chunked transfer-encoding."""

    def __init__(self, wfile: SupportsWrite[bytes], buffer_size: int = 32 * 1024) -> None:
        self._wfile = wfile
        self._buffer = bytearray()
        self._buffer_size = buffer_size

    def write(self, data: bytes) -> int:
        self._buffer += data
        if len(self._buffer) >= self._buffer_size:
            self._flush()
        return len(data)

    def flush(self) -> None:
        # zipfile.close() calls fp.flush(); chunks are coalesced until close().
        pass

    def _flush(self) -> None:
        if self._buffer:
            self._wfile.write(_http1.chunk(bytes(self._buffer)))
            self._buffer.clear()

    def close(self) -> None:
        self._flush()
        self._wfile.write(_http1.CHUNK_TERMINATOR)


class ServeryHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP/1.1 file-serving handler with servery's safety, listing, and ranges."""

    protocol_version = "HTTP/1.1"
    server_version = f"servery/{__version__}"
    index_pages = ("index.html", "index.htm")
    error_message_format = _ERROR_TEMPLATE  # styled, on-brand error pages
    _body_remaining: int | None = None
    _body_offset: int = 0
    _generated_page: bool = False
    _vary_accept_encoding: bool = False  # emit Vary: Accept-Encoding (compressible resource)
    _access_status: int | str = "-"  # captured per response for the access log
    _access_size: int | str = "-"
    _capture_len: bool = False  # set per response: is an access log configured?
    _version_string_cache: ClassVar[str | None] = None  # the Server header is constant
    # Our parse_request() populates these (replacing the email-based parser).
    headers: _RequestHeaders
    command: str | None  # may be None on a malformed first line
    raw_requestline: bytes

    @property
    def _server(self) -> ServeryHTTPServer:
        return cast("ServeryHTTPServer", self.server)

    def date_time_string(self, timestamp: float | None = None) -> str:
        # Last-Modified (timestamp given) still formats per file; the current-time
        # Date header (no timestamp) comes from the per-second process-wide cache.
        if timestamp is not None:
            return _http1.format_http_date(timestamp)
        return _http1.http_date()

    def setup(self) -> None:
        super().setup()
        # A default socket timeout bounds slow/idle clients (Slowloris).
        self.connection.settimeout(self._server.config.timeout)
        # Disable Nagle: response headers and body go out as separate writes, so
        # Nagle + delayed-ACK adds a ~40 ms stall to every small response.
        with contextlib.suppress(OSError):
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def handle(self) -> None:
        if self._server.config.http2 and self._is_http2():
            from servery.http2.connection import H2Connection

            H2Connection(self).run()
            return
        super().handle()

    def _is_http2(self) -> bool:
        sock = self.connection
        if isinstance(sock, ssl.SSLSocket):
            return sock.selected_alpn_protocol() == "h2"
        try:
            # h2c prior-knowledge: the client opens with the connection preface.
            return cast("io.BufferedReader", self.rfile).peek(24).startswith(b"PRI * HTTP/2.0")
        except (OSError, ValueError):  # pragma: no cover - peek unsupported/closed
            return False

    # --- request parsing -------------------------------------------------

    def parse_request(self) -> bool:
        """Parse the request line and headers.

        Faithful to the stdlib, but headers go through a fast line-based reader
        instead of ``http.client.parse_headers`` — the email module spends most
        of a small request's CPU doing MIME work that HTTP never needs.
        """
        self.command = None  # set in case of error on the first line
        self.request_version = version = self.default_request_version
        self.close_connection = True
        requestline = str(self.raw_requestline, "iso-8859-1").rstrip("\r\n")
        self.requestline = requestline
        words = requestline.split()
        if not words:
            return False

        if len(words) >= 3:  # the version is present
            version = words[-1]
            if not self._accept_http_version(version):
                return False
            self.request_version = version

        if not 2 <= len(words) <= 3:
            self.send_error(HTTPStatus.BAD_REQUEST, f"Bad request syntax ({requestline!r})")
            return False
        command, path = words[:2]
        if len(words) == 2:
            self.close_connection = True
            if command != "GET":
                self.send_error(HTTPStatus.BAD_REQUEST, f"Bad HTTP/0.9 request type ({command!r})")
                return False
            self.command, self.path, self.headers = command, path, _RequestHeaders([])
            return True
        self.command, self.path = command, path

        # gh-87389: collapse a leading "//" so a client can't read the path as an
        # absolute "//authority" URI (open-redirect protection).
        if self.path.startswith("//"):
            self.path = "/" + self.path.lstrip("/")

        try:
            self.headers = _read_request_headers(self.rfile)
        except _HeaderError as err:
            self.send_error(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, str(err))
            return False

        conntype = self.headers.get("Connection", "")
        if conntype.lower() == "close":
            self.close_connection = True
        elif conntype.lower() == "keep-alive" and self.protocol_version >= "HTTP/1.1":
            self.close_connection = False
        expect = self.headers.get("Expect", "")
        if (
            expect.lower() == "100-continue"
            and self.protocol_version >= "HTTP/1.1"
            and self.request_version >= "HTTP/1.1"
        ):
            return self.handle_expect_100()
        return True

    def _accept_http_version(self, version: str) -> bool:
        """Validate the request version; send an error and return False if bad."""
        # Fast path for the only two versions a real HTTP/1.x client sends, so the
        # hot path skips the split/isdigit/int parsing below.
        if version == "HTTP/1.1":
            if self.protocol_version >= "HTTP/1.1":
                self.close_connection = False
            return True
        if version == "HTTP/1.0":
            return True
        try:
            if not version.startswith("HTTP/"):
                raise ValueError
            base = version.split("/", 1)[1]
            parts = base.split(".")
            if (
                len(parts) != 2
                or any(not p.isdigit() for p in parts)
                or any(len(p) > 10 for p in parts)
            ):
                raise ValueError
            number = (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            self.send_error(HTTPStatus.BAD_REQUEST, f"Bad request version ({version!r})")
            return False
        if number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
            self.close_connection = False
        if number >= (2, 0):
            self.send_error(HTTPStatus.HTTP_VERSION_NOT_SUPPORTED, f"Invalid HTTP version ({base})")
            return False
        return True

    # --- path safety -----------------------------------------------------

    def translate_path(self, path: str) -> str:
        fs_path = super().translate_path(path)
        # Fail closed: a path escaping the root (e.g. via a symlink) becomes the
        # empty string, which open() turns into a 404.
        if security.is_contained(self._server.root_real, fs_path):
            return fs_path
        return ""

    # --- GET / HEAD ------------------------------------------------------

    def send_head(self) -> BinaryIO | None:
        self._body_remaining = None
        self._body_offset = 0
        self._generated_page = False
        self._vary_accept_encoding = False
        if not self._authorized():
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return self._serve_directory(path)
        # Check the (rare) SPA flag first so the os.path.exists() stat is skipped
        # entirely on the common, non-SPA path.
        if self._server.config.spa and not os.path.exists(path):
            index = os.path.join(self._server.root_real, "index.html")
            if os.path.isfile(index):
                return self._serve_file(index)
        return self._serve_file(path)

    def _serve_directory(self, path: str) -> BinaryIO | None:
        # Redirect to add the trailing slash so relative links resolve.
        parts = urllib.parse.urlsplit(self.path)
        if not parts.path.endswith(("/", "%2f", "%2F")):
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header(
                "Location", urllib.parse.urlunsplit(parts._replace(path=parts.path + "/"))
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        query = urllib.parse.parse_qs(parts.query)
        archive_format = query.get("archive", [""])[0]
        if archive_format in {"tar.gz", "zip"}:
            return self._serve_archive(path, archive_format)
        selected = query.get("sel")  # checkboxes from the listing -> zip of those entries
        if selected:
            return self._serve_selection(path, selected)
        # Index lookup goes through the SAME containment check as everything else:
        # an index.html symlinked outside the root must not be served.
        for name in self.index_pages:
            candidate = os.path.join(path, name)
            if os.path.isfile(candidate) and security.is_contained(
                self._server.root_real, candidate
            ):
                return self._serve_file(candidate)
        return self.list_directory(path)

    def _serve_selection(self, path: str, names: list[str]) -> None:
        """Stream the checkbox-selected entries of ``path`` as one zip."""
        base_name = os.path.basename(path.rstrip("/" + os.sep)) or "selection"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", _content_disposition(f"{base_name}.zip"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if self.command == "HEAD":
            return
        writer = _ChunkedWriter(self.wfile)
        try:
            archive.stream_zip_selection(path, names, base_name, writer)
            writer.close()
        except OSError as exc:  # pragma: no cover - client hung up, or a file changed
            _log.logger.debug("selection zip aborted: %r", exc)
            self.close_connection = True

    def _serve_archive(self, path: str, archive_format: str) -> None:
        base_name = os.path.basename(path.rstrip("/" + os.sep)) or "archive"
        filename = f"{base_name}.{archive_format}"
        content_type = "application/gzip" if archive_format == "tar.gz" else "application/zip"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", _content_disposition(filename))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if self.command == "HEAD":
            return
        writer = _ChunkedWriter(self.wfile)
        try:
            if archive_format == "tar.gz":
                archive.stream_targz(path, base_name, writer)
            else:
                archive.stream_zip(path, base_name, writer)
            writer.close()
        except OSError as exc:  # pragma: no cover - client hung up, or file changed mid-walk
            # The chunked body is partly sent and unrecoverable; close the
            # connection so the client gets a definite end-of-message rather than
            # a truncated, terminator-less body.
            _log.logger.debug("archive stream aborted: %r", exc)
            self.close_connection = True
        return

    def _maybe_proxy(self) -> bool:
        """Forward the request to an upstream if a ``--proxy`` route matches."""
        routes = self._server.config.proxy_routes
        if not routes:
            return False
        from servery import _proxy

        target = _proxy.target_for(self.path, routes)
        if target is None:
            return False
        if not self._authorized():  # --auth gates proxied routes too (401 already sent)
            return True
        _proxy.forward(self, target)
        return True

    def _proxy_or_unsupported(self) -> None:
        if not self._maybe_proxy():
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command})")

    def do_GET(self) -> None:
        if self._maybe_proxy():
            return
        f = self.send_head()
        if f is None:
            return
        try:
            self._send_body(f)
        finally:
            f.close()

    def do_HEAD(self) -> None:
        if self._maybe_proxy():
            return
        f = self.send_head()
        if f is not None:
            f.close()

    # --- WebDAV (v1.3, opt-in --dav) -------------------------------------

    def _dav(self, op: str, *, write: bool) -> None:
        """Dispatch a WebDAV method, gated by --dav / --dav-write and auth."""
        config = self._server.config
        if not config.dav:
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command})")
            return
        if not self._authorized():  # 401 already sent
            return
        if write and not config.dav_write:
            self.send_error(HTTPStatus.FORBIDDEN, "WebDAV is read-only (enable --dav-write)")
            return
        from servery import _webdav

        _webdav.dispatch(self, op)

    def do_PROPFIND(self) -> None:
        self._dav("propfind", write=False)

    def do_PROPPATCH(self) -> None:
        self._dav("proppatch", write=True)

    def do_MKCOL(self) -> None:
        self._dav("mkcol", write=True)

    def do_COPY(self) -> None:
        self._dav("copy", write=True)

    def do_MOVE(self) -> None:
        self._dav("move", write=True)

    def do_LOCK(self) -> None:
        self._dav("lock", write=False)

    def do_UNLOCK(self) -> None:
        self._dav("unlock", write=False)

    def do_PUT(self) -> None:
        config = self._server.config
        if config.dav:  # WebDAV owns PUT when mounted
            self._dav("put", write=True)
            return
        if self._maybe_proxy():
            return
        if config.upload:  # resumable Content-Range PUT (the --upload write API)
            self._resumable_put()
            return
        self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command})")

    def do_DELETE(self) -> None:
        if self._server.config.dav:
            self._dav("delete", write=True)
        else:
            self._proxy_or_unsupported()

    def do_PATCH(self) -> None:
        self._proxy_or_unsupported()

    # --- upload (v0.6) ---------------------------------------------------

    def do_POST(self) -> None:
        if self._maybe_proxy():
            return
        self._generated_page = False
        if not self._authorized():
            return
        config = self._server.config
        if not config.upload:
            self.send_error(HTTPStatus.NOT_FOUND, "Upload is not enabled")
            return
        dest_dir = self.translate_path(self.path)
        if not os.path.isdir(dest_dir) or not security.is_contained(
            self._server.root_real, dest_dir
        ):
            self.send_error(HTTPStatus.NOT_FOUND, "Upload directory not found")
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected multipart/form-data")
            return
        boundary = upload.extract_boundary(content_type)
        if boundary is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing multipart boundary")
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_error(HTTPStatus.LENGTH_REQUIRED, "Content-Length required for upload")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if length < 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if length > config.max_upload_size:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return

        reader = upload.BoundedReader(self.rfile, length)
        try:
            upload.save(
                reader,
                boundary,
                dest_dir,
                allow_overwrite=config.allow_overwrite,
                extract=config.upload_extract,
                max_upload_size=config.max_upload_size,
            )
        except upload.UploadConflictError:
            self.send_error(HTTPStatus.CONFLICT, "A file with that name already exists")
            return
        except upload.UploadError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Malformed upload")
            return
        reader.drain()  # keep the connection aligned for keep-alive
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- resumable upload (Content-Range PUT, opt-in --upload) -----------

    def _resumable_put(self) -> None:
        """Write/append a file via PUT, resumably (the S3/GCS Content-Range pattern)."""
        if not self._authorized():
            return
        config = self._server.config
        target = self.translate_path(self.path)
        url_path = urllib.parse.urlsplit(self.path).path
        # The URL must name a file inside the served root with an existing parent.
        if not target or url_path.endswith("/"):
            self._put_reject(HTTPStatus.FORBIDDEN, "PUT target must be a file path")
            return
        if os.path.isdir(target):
            self._put_reject(HTTPStatus.CONFLICT, "Target is a directory")
            return
        if not os.path.isdir(os.path.dirname(target)) or not security.is_contained(
            self._server.root_real, target
        ):
            self._put_reject(HTTPStatus.NOT_FOUND, "Upload directory not found")
            return

        length = self._put_content_length()
        if length is None:
            return  # an error was already sent
        if length > config.max_upload_size:
            self._put_reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return

        range_header = self.headers.get("Content-Range")
        if range_header is None:  # a plain PUT writes the whole body
            self._put_whole(target, length)
            return
        try:
            content_range = _resumable.parse_content_range(range_header)
        except _resumable.ResumableError as exc:
            self._put_reject(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._put_ranged(target, content_range, length)

    def _put_content_length(self) -> int | None:
        """Parse a required, non-negative Content-Length, or send an error and return None."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._put_reject(HTTPStatus.LENGTH_REQUIRED, "Content-Length required for upload")
            return None
        try:
            length = int(raw)
        except ValueError:
            length = -1
        if length < 0:
            self._put_reject(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        return length

    def _put_ranged(self, target: str, cr: _resumable.ContentRange, length: int) -> None:
        config = self._server.config
        if cr.total is not None and cr.total > config.max_upload_size:
            self._put_reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return
        part = _resumable.part_path(target)
        stored = _resumable.stored_bytes(part)
        if cr.is_query:  # "bytes */total": report how far we got, no body to read
            self._put_incomplete(stored)
            return
        if cr.length != length:
            self._put_reject(HTTPStatus.BAD_REQUEST, "Content-Length must match Content-Range")
            return
        if cr.start != stored:  # a gap/overlap: the body is unread, so close and resync
            self.close_connection = True
            self._put_incomplete(stored, status=HTTPStatus.CONFLICT)
            return
        if stored + cr.length > config.max_upload_size:
            self._put_reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return
        reader = upload.BoundedReader(self.rfile, length)
        try:
            written = _resumable.append(part, reader, length)
        except OSError:
            self._put_reject(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not write upload")
            return
        reader.drain()
        new_offset = stored + written
        if cr.total is not None and new_offset >= cr.total:
            self._put_commit(part, target)
        else:
            self._put_incomplete(new_offset)

    def _put_whole(self, target: str, length: int) -> None:
        existed = os.path.exists(target)
        if existed and not self._server.config.allow_overwrite:
            self._put_reject(HTTPStatus.CONFLICT, "A file with that name already exists")
            return
        reader = upload.BoundedReader(self.rfile, length)
        try:
            _resumable.write_whole(target, reader, length)
        except OSError:
            self._put_reject(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not write upload")
            return
        reader.drain()
        self._put_created(existed)

    def _put_commit(self, part: str, target: str) -> None:
        existed = os.path.exists(target)
        if existed and not self._server.config.allow_overwrite:
            # The body was already consumed (aligned) — keep the completed sidecar so
            # an --allow-overwrite retry can finish; just signal the conflict.
            self.send_error(HTTPStatus.CONFLICT, "A file with that name already exists")
            return
        try:
            _resumable.commit(part, target)
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not finalize upload")
            return
        self._put_created(existed)

    def _put_created(self, existed: bool) -> None:
        self.send_response(HTTPStatus.OK if existed else HTTPStatus.CREATED)
        if not existed:
            self.send_header("Location", self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _put_incomplete(self, offset: int, *, status: int = 308) -> None:
        """A 308 'Resume Incomplete' (Google convention) reporting bytes stored."""
        self.send_response(status, "Resume Incomplete")
        if offset > 0:
            self.send_header("Range", f"bytes=0-{offset - 1}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _put_reject(self, code: int, message: str) -> None:
        # The request body was not consumed; close so keep-alive can't desync.
        self.close_connection = True
        self.send_error(code, message)

    def _serve_file(self, path: str) -> BinaryIO | None:
        try:
            f = open(path, "rb")  # noqa: SIM115 (handed to the caller / closed on error)
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            stat = os.fstat(f.fileno())
            size = stat.st_size
            last_modified = self.date_time_string(stat.st_mtime)
            # Declare UTF-8 on text types so browsers don't mis-decode them (e.g. a
            # .md/.txt with em dashes or emoji rendered as mojibake).
            ctype = _compress.with_charset(self.guess_type(path))
            cache_control = self._server.config.cache_control
            # ?download=1 forces a save dialog instead of inline rendering. The
            # substring pre-check skips urlsplit+parse_qs (the common case has no
            # query at all); "download" not in the path => it can't be a query key.
            download = "download" in self.path and "download" in urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query
            )
            disposition = _content_disposition(os.path.basename(path)) if download else None

            range_header = self.headers.get("Range")
            # Compression and ranges are mutually exclusive: a Range over the
            # *encoded* bytes is incoherent on the fly, so we only compress when no
            # Range is asked for (RFC 9110 §14.1.2). Compressible resources always
            # advertise Vary: Accept-Encoding so a shared cache can't mix codings
            # (§12.5.5). The coding is zstd (3.14+) when offered and accepted, else
            # gzip, else None — one shared decision (see _compress.choose_encoding).
            self._vary_accept_encoding = _compress.compressible(ctype)
            coding = (
                None
                if range_header
                else _compress.choose_encoding(
                    ctype,
                    size,
                    self.headers.get("Accept-Encoding", ""),
                    enabled=self._server.config.compress,
                )
            )
            # The coded representation needs a distinct (still strong) ETag (§8.8.3.3);
            # decide the coding BEFORE conditionals so a 304/If-None-Match echoes the
            # tag for the representation the client would actually get.
            etag = _conditional.coding_variant(_conditional.make_etag(stat), coding)

            if self._is_not_modified(etag, stat.st_mtime):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                f.close()
                return None

            if coding is not None:
                body = _compress.encode(f.read(), coding)
                f.close()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", cache_control)
                self.send_header("Content-Encoding", coding)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                if disposition is not None:
                    self.send_header("Content-Disposition", disposition)
                self.end_headers()  # no Accept-Ranges: a coded body isn't byte-rangeable
                self._body_remaining = len(body)
                return io.BytesIO(body)

            if range_header and not self._if_range_ok(etag, stat.st_mtime):
                range_header = None
            requested = ranges.parse(range_header, size)

            if requested is ranges.UNSATISFIABLE:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                f.close()
                return None

            if isinstance(requested, ranges.ByteRange):
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", cache_control)
                self.send_header("Content-Range", f"bytes {requested.start}-{requested.end}/{size}")
                self.send_header("Content-Length", str(requested.length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self._send_repr_digest(path)
                if disposition is not None:
                    self.send_header("Content-Disposition", disposition)
                self.end_headers()
                f.seek(requested.start)
                self._body_remaining = requested.length
                self._body_offset = requested.start
                return f

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self._send_repr_digest(path)
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            # Pass the exact length so socket.sendfile sends it in one syscall
            # (count=None makes it loop to EOF + fstat for the size).
            self._body_remaining = size
            return f
        except BaseException:
            f.close()
            raise

    def _send_body(self, source: BinaryIO) -> None:
        count = self._body_remaining
        if count == 0:
            return  # socket.sendfile treats count==0 as "whole file"; never that
        sock = self.connection
        # Zero-copy fast path for plain sockets. (socket.sendfile transparently
        # handles non-regular sources like BytesIO via its own send loop; TLS
        # sockets cannot sendfile, so they take the userspace path below.)
        # Skip it where os.sendfile is absent (Windows): socket.sendfile would fall
        # back to a slow 8 KiB send loop — take the userspace copy below instead.
        if _HAS_SENDFILE and not isinstance(sock, ssl.SSLSocket):
            # The offset is already known (0, or the range start), so we avoid a
            # source.tell() lseek on every request.
            offset = self._body_offset
            try:
                sock.sendfile(source, offset, count)
                return
            except (OSError, ValueError):
                # If bytes were already sent the stream is broken — re-raise
                # rather than resend (which would overrun a range). Only retry in
                # userspace when nothing went out.
                if source.tell() != offset:
                    raise
        # Userspace copy: TLS sockets, or plain sockets on no-sendfile platforms
        # (Windows). A plain socket sends each write in one syscall, so it gets the
        # large raw buffer; TLS re-chunks to its record size and keeps the default.
        bufsize = _COPY_BUFSIZE if isinstance(sock, ssl.SSLSocket) else _RAW_COPY_BUFSIZE
        if count is None:
            shutil.copyfileobj(source, self.wfile, bufsize)
        else:
            _copy_n(source, self.wfile, count, bufsize)

    # --- authentication --------------------------------------------------

    def _authorized(self) -> bool:
        credential = self._server.credential
        if credential is None:
            return True
        header = self.headers.get("Authorization")
        if header is not None and credential.check_header(header):
            return True
        # Close the connection: a rejected request may carry an unread body
        # (e.g. a POST) which would otherwise be mis-parsed as the next request.
        self.close_connection = True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", auth.WWW_AUTHENTICATE)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        return False

    # --- conditional requests -------------------------------------------

    def _is_not_modified(self, etag: str, mtime: float) -> bool:
        return _conditional.is_not_modified(
            etag,
            mtime,
            if_none_match=self.headers.get("If-None-Match"),
            if_modified_since=self.headers.get("If-Modified-Since"),
        )

    def _send_repr_digest(self, path: str) -> None:
        """Emit an RFC 9530 ``Repr-Digest`` over the full file if the client asked.

        Only on identity (un-coded) responses, where the representation *is* the file
        on disk; computed lazily (it reads the whole file) so the default download
        path pays nothing. Covers a parallel/ranged download: the digest is over the
        whole representation, so a client can verify the reassembled result.
        """
        algorithm = _digest.choose_algorithm(self.headers.get("Want-Repr-Digest"))
        if algorithm is None:
            return
        value = _digest.field_value_for_file(path, algorithm)
        if value is not None:
            self.send_header("Repr-Digest", value)

    def _if_range_ok(self, etag: str, mtime: float) -> bool:
        condition = self.headers.get("If-Range")
        if condition is None:
            return True
        condition = condition.strip()
        if condition.startswith(('"', "W/")):
            return condition == etag  # strong comparison
        return _conditional.not_modified_since(condition, mtime)

    # --- directory listing (v0.2) ---------------------------------------

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        self._generated_page = True
        parts = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parts.query)
        sort = listing.code_to_sort(params.get("C", ["N"])[0])
        order = "desc" if params.get("O", ["A"])[0] == "D" else "asc"
        query = params.get("q", [""])[0]
        ext = params.get("ext", [""])[0]
        try:
            page = max(1, int(params.get("page", ["1"])[0]))
        except ValueError:
            page = 1
        # Theme: an explicit ?theme= wins and is persisted in a cookie; otherwise
        # fall back to the cookie, then "auto". No JavaScript involved.
        theme_param = params.get("theme", [None])[0]
        set_theme_cookie = theme_param in {"auto", "light", "dark"}
        theme = theme_param if set_theme_cookie else self._theme_cookie()
        display = urllib.parse.unquote(parts.path, errors="surrogatepass")
        try:
            body = listing.render(
                os.fspath(path),
                display,
                show_hidden=self._server.config.show_hidden,
                sort=sort,
                order=order,
                query=query,
                ext=ext,
                page=page,
                per_page=listing.DEFAULT_PAGE_SIZE,
                theme=theme,
                upload=self._server.config.upload,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
        # The listing is generated HTML — always compressible (and Vary-keyed).
        self._vary_accept_encoding = True
        encoding = _compress.negotiate(
            self.headers.get("Accept-Encoding", ""), enabled=self._server.config.compress
        )
        if encoding is not None:
            body = _compress.encode(body, encoding)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if encoding is not None:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        if set_theme_cookie:
            # Lax + one-year; the value is one of three literals so it is safe.
            self.send_header(
                "Set-Cookie",
                f"servery_theme={theme}; Path=/; Max-Age=31536000; SameSite=Lax",
            )
        self.end_headers()
        return io.BytesIO(body)

    def _theme_cookie(self) -> str:
        """Return the persisted theme from the request cookie, or "auto"."""
        raw = self.headers.get("Cookie")
        if not raw:
            return "auto"
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return "auto"
        morsel = jar.get("servery_theme")
        if morsel is not None and morsel.value in {"auto", "light", "dark"}:
            return morsel.value
        return "auto"

    # --- universal response shaping -------------------------------------

    def end_headers(self) -> None:
        config = self._server.config
        if config.security_headers:
            # nosniff everywhere (we serve arbitrary files); CSP + Referrer-Policy
            # only on servery-generated HTML; HSTS only over TLS.
            self.send_header("X-Content-Type-Options", "nosniff")
            if self._generated_page:
                self.send_header("Content-Security-Policy", _CSP)
                self.send_header("Referrer-Policy", "no-referrer")
            if isinstance(self.connection, ssl.SSLSocket):
                self.send_header("Strict-Transport-Security", "max-age=63072000")
        if config.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if self._vary_accept_encoding:
            self.send_header("Vary", "Accept-Encoding")
        super().end_headers()
        access = self._server.access_log
        if access is not None:
            access.record(
                self.address_string(),
                getattr(self, "requestline", "-"),
                self._access_status,
                self._access_size,
                referer=self.headers.get("Referer", "-") if self.headers else "-",
                user_agent=self.headers.get("User-Agent", "-") if self.headers else "-",
            )

    def send_response_only(self, code: int, message: str | None = None) -> None:
        self._access_status = code  # captured for the access log (size set in send_header)
        self._access_size = "-"
        # Resolve "is an access log configured?" once per response, not per header.
        self._capture_len = self._server.access_log is not None
        super().send_response_only(code, message)

    def send_header(self, keyword: str, value: str) -> None:
        # Only pay the per-header check when an access log will consume the size.
        if self._capture_len and keyword.lower() == "content-length":
            self._access_size = value
        super().send_header(keyword, value)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._generated_page = True  # the error body is generated HTML
        super().send_error(code, message, explain)

    def do_OPTIONS(self) -> None:
        if self._maybe_proxy():
            return
        self._generated_page = False
        config = self._server.config
        # Preflight must succeed without auth, or the real request never happens.
        self.send_response(HTTPStatus.NO_CONTENT)
        if config.cors:
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        if config.dav:
            from servery import _webdav

            # Class 2 (with the stub lock) so Finder/Windows mount read-write.
            self.send_header("DAV", "1, 2")
            self.send_header("MS-Author-Via", "DAV")
            self.send_header("Allow", _webdav._ALLOW_RW if config.dav_write else _webdav._ALLOW_RO)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def version_string(self) -> str:
        # The Server header (server_version + sys_version) is constant; build once.
        cached = ServeryHandler._version_string_cache
        if cached is None:
            cached = ServeryHandler._version_string_cache = super().version_string()
        return cached

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base signature)
        # Route through the logging module. Guard on the level so we don't format
        # the line (or call address_string) when logging is disabled (quiet mode).
        if _log.logger.isEnabledFor(logging.INFO):
            _log.logger.info("%s %s", self.address_string(), format % args)


_MAX_HEADER_LINE = 65536  # bytes per line; matches http.client._MAXLINE
_MAX_HEADER_COUNT = 100  # matches http.client._MAXHEADERS


class _HeaderError(Exception):
    """An over-long header line or too many headers (-> 431)."""


class _RequestHeaders:
    """Minimal case-insensitive request-header map (first occurrence wins).

    A fast stand-in for ``email.message.Message``: the handler only ever calls
    ``.get()``, and email's MIME parsing is most of a small request's CPU.
    """

    __slots__ = ("_map", "_pairs")

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs
        mapping: dict[str, str] = {}
        for name, value in pairs:
            key = name.lower()
            if key not in mapping:  # first wins, matching email.Message.get
                mapping[key] = value
        self._map = mapping

    @overload
    def get(self, name: str) -> str | None: ...
    @overload
    def get(self, name: str, default: str) -> str: ...
    def get(self, name: str, default: str | None = None) -> str | None:
        return self._map.get(name.lower(), default)

    def __getitem__(self, name: str) -> str | None:  # email.Message returns None, not KeyError
        return self._map.get(name.lower())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._map

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


def _read_request_headers(rfile: io.BufferedIOBase) -> _RequestHeaders:
    """Read the header block as ``(name, value)`` pairs (RFC 9112 §5).

    Enforces the same line/count limits as ``http.client``. ``obs-fold``
    continuations are folded into a single space (RFC 9112 §5.2).
    """
    pairs: list[tuple[str, str]] = []
    while True:
        line = rfile.readline(_MAX_HEADER_LINE + 1)
        if len(line) > _MAX_HEADER_LINE:
            raise _HeaderError("Header line too long")
        if line in (b"\r\n", b"\n", b""):
            break
        if line[:1] in (b" ", b"\t"):  # obs-fold continuation
            if pairs:
                name, value = pairs[-1]
                pairs[-1] = (name, f"{value} {line.strip().decode('latin-1')}")
            continue
        if len(pairs) >= _MAX_HEADER_COUNT:
            raise _HeaderError("Too many headers")
        name, sep, value = line.partition(b":")
        if not sep:
            continue  # a line without a colon is not a header field; ignore it
        pairs.append((name.decode("latin-1").strip(), value.strip().decode("latin-1")))
    return _RequestHeaders(pairs)

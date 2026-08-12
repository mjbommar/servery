"""The servery request handler.

We subclass the stdlib ``SimpleHTTPRequestHandler`` rather than reimplementing
HTTP: the base gives us the buffered request loop, HEAD/GET dispatch, directory
redirects, and MIME typing. servery overrides what it improves:

* ``parse_request`` — adapts the shared strict HTTP/1 request-head policy;
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
import http.server
import io
import logging
import os
import shutil
import socket
import ssl
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, BinaryIO, ClassVar, cast

from servery import (
    __version__,
    _body,
    _compress,
    _digest,
    _http1,
    _log,
    _request,
    _resumable,
    _static,
    _work,
    _write,
    archive,
    auth,
    listing,
    security,
    upload,
)

_HeaderError = _request.HeaderError
_RequestHeaders = _request.RequestHeaders
_read_request_headers = _request.read_headers

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


_content_disposition = _static.content_disposition

# Query-flag values that mean "off", so ?preview=0 does not enable the preview.
_FALSEY = frozenset(("0", "false", "no", "off", ""))


def _enabled(values: list[str] | None) -> bool:
    """True when a query flag is present with a value that isn't explicitly off."""
    return bool(values) and values[0].lower() not in _FALSEY


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
    _csp: str | None = None  # per-response CSP override (the preview page widens it)
    _vary_accept_encoding: bool = False  # emit Vary: Accept-Encoding (compressible resource)
    _access_status: int | str = "-"  # captured per response for the access log
    _access_size: int | str = "-"
    _capture_len: bool = False  # set per response: is an access log configured?
    _body_plan = _body.BodyPlan(0)
    _body_forced_close: bool = False
    _body_original_close: bool = True
    _requests_served: int = 0
    _request_limit_close: bool = False
    _version_string_cache: ClassVar[str | None] = None  # the Server header is constant
    # Our parse_request() populates these (replacing the email-based parser).
    headers: _RequestHeaders
    command: str | None  # may be None on a malformed first line
    raw_requestline: bytes
    _headers_buffer: list[bytes]

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
        write_timeout = self._server.config.write_timeout
        if write_timeout is not None:
            self.wfile = _write.DeadlineWriter(
                cast("BinaryIO", self.wfile), self.connection, write_timeout
            )

    def handle(self) -> None:
        if self._server.config.http2 and self._is_http2():
            from servery.http2.connection import H2Connection

            H2Connection(self).run()
            return
        self._handle_http1()

    def _handle_http1(self) -> None:
        """Run HTTP/1, optionally separating keep-alive idle time from active I/O."""
        config = self._server.config
        idle_timeout = config.keepalive_timeout
        head_timeout = config.request_head_timeout
        if self._server.is_draining:
            return

        if head_timeout is not None:
            self._handle_http1_with_head_deadline(head_timeout, idle_timeout)
            return

        # Mirror BaseHTTPRequestHandler.handle(), but wait for only the first byte
        # of a subsequent request under the idle budget. Once activity begins the
        # existing active socket timeout applies to request parsing/body/response.
        self.close_connection = True
        self.handle_one_request()
        while not self.close_connection:
            if self._server.is_draining:
                self.close_connection = True
                return
            if idle_timeout is not None:
                self.connection.settimeout(idle_timeout)
                try:
                    cast("io.BufferedReader", self.rfile).peek(1)
                except TimeoutError:
                    self.close_connection = True
                    return
                finally:
                    with contextlib.suppress(OSError):
                        self.connection.settimeout(config.timeout)
            self.handle_one_request()

    def _handle_http1_with_head_deadline(
        self,
        head_timeout: float,
        idle_timeout: float | None,
    ) -> None:
        """Run configured request-head deadlines without changing the default loop."""
        config = self._server.config
        source = cast("io.BufferedReader", self.rfile)
        self.close_connection = True
        first_request = True
        while True:
            # First-byte wait is an idle/progress policy, not part of the total
            # head budget. Buffered pipelined input returns from peek immediately.
            use_idle_timeout = not first_request and idle_timeout is not None
            if use_idle_timeout:
                self.connection.settimeout(idle_timeout)
            try:
                buffered = source.peek(1)
                if not buffered:
                    self.close_connection = True
                    return
            except TimeoutError:
                self.close_connection = True
                return
            finally:
                if use_idle_timeout:
                    with contextlib.suppress(OSError):
                        self.connection.settimeout(config.timeout)

            self._handle_one_request_with_head_deadline(source, head_timeout, buffered)
            if self.close_connection or self._server.is_draining:
                self.close_connection = True
                return
            first_request = False

    def _handle_one_request_with_head_deadline(
        self,
        source: io.BufferedReader,
        timeout: float,
        buffered: bytes,
    ) -> None:
        """Mirror the stdlib dispatcher with a deadline-aware head reader."""
        reader = _request.HeadDeadlineReader(source, self.connection, timeout, buffered)
        try:
            complete_head = reader.buffered_head()
            head_source = io.BytesIO(complete_head) if complete_head is not None else reader
            self.raw_requestline = head_source.readline(_request.MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > _request.MAX_REQUEST_LINE:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return

            # parse_request() deliberately keeps its default hot path unchanged.
            # The configured-only adapter temporarily supplies the bounded line
            # source; it never consumes beyond the terminating blank line, so
            # body/pipeline bytes remain in the BufferedReader restored below.
            original = self.rfile
            self.rfile = cast("io.BufferedIOBase", head_source)
            try:
                accepted = self.parse_request()
            finally:
                self.rfile = original
            if not accepted:
                return
            method_name = "do_" + cast("str", self.command)
            if not hasattr(self, method_name):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    f"Unsupported method ({self.command!r})",
                )
                return
            getattr(self, method_name)()
            self.wfile.flush()
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True

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
        self.request_version = self.default_request_version
        self.close_connection = True
        self._body_forced_close = False
        self._body_original_close = True
        try:
            parsed = _request.parse_request_line(
                self.raw_requestline,
                protocol_version=self.protocol_version,
                default_request_version=self.default_request_version,
            )
        except _request.RequestLineError as err:
            if err.response_version is not None:
                self.request_version = err.response_version
            self.close_connection = err.close_connection
            self.requestline = self.raw_requestline.decode("iso-8859-1").rstrip("\r\n")
            self.send_error(err.status, err.message)
            return False
        if parsed is None:
            return False
        self.requestline = parsed.requestline
        self.command = parsed.method
        self.path = parsed.target
        self.request_version = parsed.version
        self.close_connection = parsed.close_connection
        if not parsed.has_headers:
            self.headers = _RequestHeaders([])
            self._body_plan = _body.BodyPlan(0)
            return True

        try:
            self.headers = _read_request_headers(self.rfile)
        except _HeaderError as err:
            self.close_connection = True
            self.send_error(err.status, str(err))
            return False

        try:
            head = _request.finalize_request_head(
                parsed,
                self.headers,
                protocol_version=self.protocol_version,
            )
        except (_body.FramingError, _request.RequestHeadError) as err:
            self.close_connection = True
            self.send_error(err.status, str(err))
            return False
        self._body_plan = head.body
        self.close_connection = head.close_connection
        request_limit = self._server.config.max_requests_per_connection
        if request_limit:
            self._requests_served += 1
            if self._requests_served >= request_limit:
                self.close_connection = True
                self._request_limit_close = True
        self._body_original_close = self.close_connection
        if head.expect_continue:
            return self.handle_expect_100()
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
        self._csp = None
        self._vary_accept_encoding = False
        if not self._authorized():
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return self._serve_directory(path)
        # Opt-in ?preview= / ?metadata= views. Both flags are off by default, so
        # the common path pays two attribute reads and no parsing at all. Only an
        # existing regular file is claimed here, so the view can never shadow the
        # --spa fallback or turn a plain 404 into a different one.
        config = self._server.config
        if (config.preview or config.metadata) and "?" in self.path and os.path.isfile(path):
            handled, body = self._serve_view(path)
            if handled:
                return body
        # Check the (rare) SPA flag first so the os.path.exists() stat is skipped
        # entirely on the common, non-SPA path.
        if config.spa and not os.path.exists(path):
            index = _static.find_contained_index(
                self._server.root_real,
                self._server.root_real,
                ("index.html",),
            )
            if index is not None:
                return self._serve_file(index)
        return self._serve_file(path)

    def _serve_directory(self, path: str) -> BinaryIO | None:
        # Redirect to add the trailing slash so relative links resolve.
        location = _static.directory_redirect(self.path)
        if location is not None:
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        archive_format = query.get("archive", [""])[0]
        if archive_format in {"tar.gz", "zip"}:
            return self._serve_archive(path, archive_format)
        selected = query.get("sel")  # checkboxes from the listing -> zip of those entries
        if selected:
            return self._serve_selection(path, selected)
        # ?metadata=1 on a directory: the whole index as JSON (opt-in --metadata).
        if self._server.config.metadata and _enabled(query.get("metadata")):
            return self._serve_directory_metadata(path, parts.path)
        # Index lookup goes through the SAME containment check as everything else:
        # an index.html symlinked outside the root must not be served.
        index = _static.find_contained_index(self._server.root_real, path, self.index_pages)
        if index is not None:
            return self._serve_file(index)
        return self.list_directory(path)

    def _serve_selection(self, path: str, names: list[str]) -> None:
        """Stream the checkbox-selected entries of ``path`` as one zip."""
        base_name = os.path.basename(path.rstrip("/" + os.sep)) or "selection"
        lease = self._reserve_archive_stream()
        if lease is False:
            return
        ownership = lease if isinstance(lease, _work.WorkLease) else contextlib.nullcontext()
        with ownership:
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
                if isinstance(lease, _work.WorkLease):
                    lease.release(failed=True)
                _log.logger.debug("selection zip aborted: %r", exc)
                self.close_connection = True

    def _serve_archive(self, path: str, archive_format: str) -> None:
        base_name = os.path.basename(path.rstrip("/" + os.sep)) or "archive"
        filename = f"{base_name}.{archive_format}"
        content_type = "application/gzip" if archive_format == "tar.gz" else "application/zip"
        lease = self._reserve_archive_stream()
        if lease is False:
            return
        ownership = lease if isinstance(lease, _work.WorkLease) else contextlib.nullcontext()
        with ownership:
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
                if isinstance(lease, _work.WorkLease):
                    lease.release(failed=True)
                # The chunked body is partly sent and unrecoverable; close the
                # connection so the client gets a definite end-of-message rather than
                # a truncated, terminator-less body.
                _log.logger.debug("archive stream aborted: %r", exc)
                self.close_connection = True
        return

    def _reserve_archive_stream(self) -> _work.WorkLease | None | bool:
        """Reserve a body producer before headers; ``False`` means 503 was sent."""
        if self.command == "HEAD":
            return None
        limiter = self._server.archive_limiter
        if limiter is None:
            return None
        try:
            return limiter.reserve()
        except (_work.WorkRejectedError, _work.WorkPoolClosedError):
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

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
            self._require_body_disposition()
            self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command})")

    def do_GET(self) -> None:
        if self._maybe_proxy():
            return
        self._require_body_disposition()
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
        self._require_body_disposition()
        f = self.send_head()
        if f is not None:
            f.close()

    # --- WebDAV (v1.3, opt-in --dav) -------------------------------------

    def _dav(self, op: str, *, write: bool) -> None:
        """Dispatch a WebDAV method, gated by --dav / --dav-write and auth."""
        self._require_body_disposition()
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
        self._require_body_disposition()
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
        self._require_body_disposition()
        self._generated_page = False
        if not self._authorized():
            return
        config = self._server.config
        if not config.upload:
            self._reject_unread_body(HTTPStatus.NOT_FOUND, "Upload is not enabled")
            return
        dest_dir = self.translate_path(self.path)
        if not os.path.isdir(dest_dir) or not security.is_contained(
            self._server.root_real, dest_dir
        ):
            self._reject_unread_body(HTTPStatus.NOT_FOUND, "Upload directory not found")
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._reject_unread_body(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected multipart/form-data"
            )
            return
        boundary = upload.extract_boundary(content_type)
        if boundary is None:
            self._reject_unread_body(HTTPStatus.BAD_REQUEST, "Missing multipart boundary")
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._reject_unread_body(
                HTTPStatus.LENGTH_REQUIRED, "Content-Length required for upload"
            )
            return
        length = self._body_plan.length or 0
        if length > config.max_upload_size:
            self._reject_unread_body(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit"
            )
            return

        reader = upload.BoundedReader(self._request_body_stream(), length)
        try:
            upload.save(
                reader,
                boundary,
                dest_dir,
                allow_overwrite=config.allow_overwrite,
                extract=config.upload_extract,
                max_upload_size=config.max_upload_size,
                target_locks=self._server.target_locks,
                lock_timeout=config.write_lock_timeout,
            )
        except upload.UploadConflictError:
            self.close_connection = True
            self.send_error(HTTPStatus.CONFLICT, "A file with that name already exists")
            return
        except upload.UploadError:
            self.close_connection = True
            self.send_error(HTTPStatus.BAD_REQUEST, "Malformed upload")
            return
        if reader.drain():  # keep the connection aligned for keep-alive
            self._body_consumed()
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

        with self._server.target_locks.hold(target, config.write_lock_timeout) as acquired:
            if not acquired:
                self._put_reject(HTTPStatus.CONFLICT, "Another write to this target is active")
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
        return self._body_plan.length or 0

    def _put_ranged(self, target: str, cr: _resumable.ContentRange, length: int) -> None:
        config = self._server.config
        if cr.total is not None and cr.total > config.max_upload_size:
            self._put_reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return
        part = _resumable.part_path(target)
        if config.partial_upload_ttl and _resumable.discard_stale(part, config.partial_upload_ttl):
            self._server.partial_uploads.release(part)
        stored = _resumable.stored_bytes(part)
        if cr.is_query:  # "bytes */total": report how far we got, no body to read
            self._put_incomplete(stored)
            return
        if cr.length != length:
            self._put_reject(HTTPStatus.BAD_REQUEST, "Content-Length must match Content-Range")
            return
        if cr.start != stored:  # a gap/overlap: its body still needs a disposition
            self._put_incomplete(stored, status=HTTPStatus.CONFLICT, unread_body=True)
            return
        if stored + cr.length > config.max_upload_size:
            self._put_reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return
        if not self._server.partial_uploads.claim(part):
            self._put_reject(
                HTTPStatus.INSUFFICIENT_STORAGE, "Too many partial uploads are outstanding"
            )
            return
        reader = upload.BoundedReader(self._request_body_stream(), length)
        try:
            written = _resumable.append(part, reader, length)
        except _body.BodyTimeoutError:
            self.close_connection = True
            raise
        except OSError:
            if not os.path.exists(part):
                self._server.partial_uploads.release(part)
            self._put_reject(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Could not write upload", drain_body=False
            )
            return
        if reader.drain():
            self._body_consumed()
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
        part = _resumable.part_path(target)
        _resumable.discard(part)
        self._server.partial_uploads.release(part)
        reader = upload.BoundedReader(self._request_body_stream(), length)
        try:
            _resumable.write_whole(target, reader, length)
        except _body.BodyTimeoutError:
            _resumable.discard(part)
            self.close_connection = True
            raise
        except OSError:
            self._put_reject(
                HTTPStatus.INTERNAL_SERVER_ERROR, "Could not write upload", drain_body=False
            )
            return
        if reader.drain():
            self._body_consumed()
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
        self._server.partial_uploads.release(part)
        self._put_created(existed)

    def _put_created(self, existed: bool) -> None:
        self.send_response(HTTPStatus.OK if existed else HTTPStatus.CREATED)
        if not existed:
            self.send_header("Location", self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _put_incomplete(self, offset: int, *, status: int = 308, unread_body: bool = False) -> None:
        """A 308 'Resume Incomplete' (Google convention) reporting bytes stored."""
        self.send_response(status, "Resume Incomplete")
        if offset > 0:
            self.send_header("Range", f"bytes=0-{offset - 1}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        if unread_body:
            self._drain_rejected_put_body()

    def _put_reject(self, code: int, message: str, *, drain_body: bool = True) -> None:
        # Send the decision promptly, then apply the configurable keep-alive drain
        # policy.  Large or partially consumed bodies still close fail-safe.
        self.send_error(code, message)
        if drain_body:
            self._drain_rejected_put_body()

    def _drain_rejected_put_body(self) -> bool:
        """Drain a small rejected PUT body so its response and connection are reusable."""
        length = self._body_plan.length or 0
        limit = self._server.config.keepalive_drain_limit
        if length > limit:
            return False
        try:
            # A client may start reading as soon as it has finished sending.  Make
            # the rejection visible before spending time on the bounded drain.
            self.wfile.flush()
            if not _body.LimitedReader(self._request_body_stream(), length).drain(limit):
                return False
        except (OSError, TimeoutError):
            return False
        self._body_consumed()
        return True

    def _reject_unread_body(self, code: int, message: str) -> None:
        """Send an error and close because the declared request body remains unread."""
        self.close_connection = True
        self.send_error(code, message)

    def _require_body_disposition(self) -> None:
        """Pessimistically close unless this request's declared body is consumed."""
        if (self._body_plan.length or 0) > 0 and not self._body_forced_close:
            self._body_original_close = self.close_connection
            self._body_forced_close = True
            self.close_connection = True

    def _request_body_stream(self) -> _body.BodyStream:
        """Return this request's raw or total-deadline-bounded body stream."""
        timeout = self._server.config.request_body_timeout
        if timeout is None:
            return cast("_body.BodyStream", self.rfile)
        return _body.DeadlineReader(
            cast("_body.BodyStream", self.rfile),
            self.connection,
            timeout,
        )

    def _body_consumed(self) -> None:
        """Restore the request's original keep-alive policy after exact consumption."""
        if self._body_forced_close:
            self.close_connection = self._body_original_close
            self._body_forced_close = False

    def _serve_file(self, path: str) -> BinaryIO | None:
        ctype = _compress.with_charset(self.guess_type(path))
        range_header = self.headers.get("Range")
        try:
            opened = _static.open_file(
                path,
                ctype,
                self.headers.get("Accept-Encoding", ""),
                compression_enabled=self._server.config.compress,
                max_compress_size=self._server.config.max_compress_size,
                allow_compression=range_header is None,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        f = opened.handle
        try:
            stat = opened.stat
            size = stat.st_size
            last_modified = opened.last_modified
            cache_control = self._server.config.cache_control
            disposition = (
                _static.content_disposition(os.path.basename(path))
                if _static.download_requested(self.path)
                else None
            )

            # Compression and ranges are mutually exclusive: a Range over the
            # *encoded* bytes is incoherent on the fly, so we only compress when no
            # Range is asked for (RFC 9110 §14.1.2). Compressible resources always
            # advertise Vary: Accept-Encoding so a shared cache can't mix codings
            # (§12.5.5). The coding is zstd (3.14+) when offered and accepted, else
            # gzip, else None — one shared decision (see _compress.choose_encoding).
            self._vary_accept_encoding = _compress.compressible(ctype)
            coding = opened.coding
            # The coded representation needs a distinct (still strong) ETag (§8.8.3.3);
            # decide the coding BEFORE conditionals so a 304/If-None-Match echoes the
            # tag for the representation the client would actually get.
            etag = opened.etag
            if_none_match = self.headers.get("If-None-Match")
            if_modified_since = self.headers.get("If-Modified-Since")
            selection = (
                _static.select_identity(
                    opened,
                    range_header=range_header,
                    if_range=self.headers.get("If-Range"),
                    if_none_match=if_none_match,
                    if_modified_since=if_modified_since,
                )
                if range_header is not None
                or if_none_match is not None
                or if_modified_since is not None
                else None
            )

            if selection is not None and selection.status == HTTPStatus.NOT_MODIFIED:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                f.close()
                return None

            if coding is not None:
                key = _compress.cache_key(path, stat, coding)
                body = self._server.compression_cache.get_or_compute(
                    key, lambda: _compress.encode(f.read(), coding)
                )
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

            if (
                selection is not None
                and selection.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
            ):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", selection.content_range or "")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                f.close()
                return None

            try:
                repr_digest = self._repr_digest(opened)
            except OSError:
                # The opened identity no longer contains the byte extent whose
                # metadata would describe this response. Fail before committing
                # headers instead of emitting an unverifiable or short body.
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "File changed while hashing")
                f.close()
                return None

            if selection is not None and selection.status == HTTPStatus.PARTIAL_CONTENT:
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", cache_control)
                self.send_header("Content-Range", selection.content_range or "")
                self.send_header("Content-Length", str(selection.count))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                if repr_digest is not None:
                    self.send_header("Repr-Digest", repr_digest)
                if disposition is not None:
                    self.send_header("Content-Disposition", disposition)
                self.end_headers()
                f.seek(selection.offset)
                self._body_remaining = selection.count
                self._body_offset = selection.offset
                return f

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            if repr_digest is not None:
                self.send_header("Repr-Digest", repr_digest)
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
        small_file_buffer = self._server.config.small_file_buffer_size
        if (
            count is not None
            and small_file_buffer > 0
            and count <= small_file_buffer
            and not isinstance(sock, ssl.SSLSocket)
        ):
            source.seek(self._body_offset)
            data = source.read(count)
            if data:
                write_timeout = self._server.config.write_timeout
                if write_timeout is None:
                    sock.sendall(data)
                else:
                    with _write.socket_timeout(sock, write_timeout):
                        sock.sendall(data)
            return
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
                write_timeout = self._server.config.write_timeout
                if write_timeout is None:
                    sock.sendfile(source, offset, count)
                else:
                    with _write.socket_timeout(sock, write_timeout):
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

    def _repr_digest(self, opened: _static.FileBody) -> str | None:
        """Return an RFC 9530 digest over the opened identity if the client asked.

        Only on identity (un-coded) responses, where the representation *is* the file
        on disk; computed lazily (it reads the whole file) so the default download
        path pays nothing. Covers a parallel/ranged download: the digest is over the
        whole representation, so a client can verify the reassembled result.
        """
        algorithm = _digest.choose_algorithm(self.headers.get("Want-Repr-Digest"))
        if algorithm is None:
            return None
        key = _digest.cache_key(opened.path, opened.stat, algorithm)
        return self._server.digest_cache.get_or_compute(
            key,
            lambda: _digest.field_value_for_handle(
                opened.handle,
                algorithm,
                opened.size,
            ),
        )

    # --- directory listing (v0.2) ---------------------------------------

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        self._generated_page = True
        config = self._server.config
        options = listing.request_options(self.path, self.headers.get("Cookie"))
        meta_query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get(
            "meta", [""]
        )[0]
        try:
            body = listing.render(
                os.fspath(path),
                options.display,
                show_hidden=config.show_hidden,
                sort=options.sort,
                order=options.order,
                query=options.query,
                ext=options.ext,
                page=options.page,
                per_page=config.listing_page_size,
                theme=options.theme,
                upload=config.upload,
                max_entries=config.max_listing_entries,
                details_threshold=config.listing_details_threshold,
                metadata=config.metadata,
                meta_query=meta_query,
                meta_max_bytes=config.metadata_max_bytes,
                preview=config.preview,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
        return self._send_generated(
            body,
            "text/html; charset=utf-8",
            theme=options.theme if options.set_theme_cookie else None,
        )

    def _send_generated(
        self,
        body: bytes,
        content_type: str,
        *,
        csp: str | None = None,
        theme: str | None = None,
    ) -> io.BytesIO:
        """Send a servery-generated page: negotiated coding, CSP, optional theme cookie."""
        self._generated_page = True
        self._csp = csp
        # Generated pages are text — always compressible (and Vary-keyed).
        self._vary_accept_encoding = True
        encoding = _compress.negotiate(
            self.headers.get("Accept-Encoding", ""), enabled=self._server.config.compress
        )
        if encoding is not None:
            body = _compress.encode(body, encoding)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if encoding is not None:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        if theme is not None:
            # Lax + one-year; the value is one of three literals so it is safe.
            self.send_header(
                "Set-Cookie",
                f"servery_theme={theme}; Path=/; Max-Age=31536000; SameSite=Lax",
            )
        self.end_headers()
        return io.BytesIO(body)

    # --- preview / metadata views (opt-in) -------------------------------

    def _serve_view(self, path: str) -> tuple[bool, io.BytesIO | None]:
        """Dispatch a file's ``?preview=`` / ``?metadata=`` view; (handled, body)."""
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        config = self._server.config
        if config.metadata and _enabled(params.get("metadata")):
            return True, self._serve_file_metadata(path)
        if config.preview and _enabled(params.get("preview")):
            return True, self._serve_preview(path, params.get("preview", [""])[0])
        return False, None

    def _serve_preview(self, path: str, mode: str) -> io.BytesIO | None:
        from servery import _preview

        config = self._server.config
        display = urllib.parse.unquote(
            urllib.parse.urlsplit(self.path).path, errors="surrogatepass"
        )
        try:
            body = _preview.render(
                path,
                display,
                mode=mode if mode in ("render", "source") else "",
                max_bytes=config.preview_max_bytes,
                theme=listing.request_options(self.path, self.headers.get("Cookie")).theme,
                metadata=config.metadata,
                metadata_max_bytes=config.metadata_max_bytes,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        return self._send_generated(body, "text/html; charset=utf-8", csp=_preview.CSP)

    def _serve_file_metadata(self, path: str) -> io.BytesIO | None:
        from servery import _metadata

        display = urllib.parse.unquote(
            urllib.parse.urlsplit(self.path).path, errors="surrogatepass"
        )
        document = _metadata.describe(
            path, display, max_bytes=self._server.config.metadata_max_bytes
        )
        return self._send_generated(_metadata.to_json(document), "application/json; charset=utf-8")

    def _serve_directory_metadata(self, path: str, url_path: str) -> io.BytesIO | None:
        from servery import _metadata

        config = self._server.config
        display = urllib.parse.unquote(url_path, errors="surrogatepass")
        try:
            document = _metadata.describe_directory(
                path,
                display,
                show_hidden=config.show_hidden,
                max_bytes=config.metadata_max_bytes,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
        return self._send_generated(_metadata.to_json(document), "application/json; charset=utf-8")

    # --- universal response shaping -------------------------------------

    def end_headers(self) -> None:
        config = self._server.config
        draining = self._server.is_draining
        if (
            (self._request_limit_close or draining)
            and isinstance(self._access_status, int)
            and self._access_status >= 200
        ):
            # Connection persistence is server-owned. A CGI/proxy response must
            # not override the terminal request with ``keep-alive`` or leave
            # contradictory duplicate fields on the wire.
            self._headers_buffer[:] = [
                line for line in self._headers_buffer if not line.lower().startswith(b"connection:")
            ]
            self.send_header("Connection", "close")
            if draining:
                self.close_connection = True
        if config.security_headers:
            # nosniff everywhere (we serve arbitrary files); CSP + Referrer-Policy
            # only on servery-generated HTML; HSTS only over TLS.
            self.send_header("X-Content-Type-Options", "nosniff")
            if self._generated_page:
                self.send_header("Content-Security-Policy", self._csp or _static.GENERATED_CSP)
                self.send_header("Referrer-Policy", "no-referrer")
            if isinstance(self.connection, ssl.SSLSocket):
                self.send_header("Strict-Transport-Security", "max-age=63072000")
        if config.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if self._server.http3_port is not None:
            self.send_header("Alt-Svc", f'h3=":{self._server.http3_port}"; ma=86400')
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
        self._csp = None  # never inherit a widened policy from an earlier response
        super().send_error(code, message, explain)

    def do_OPTIONS(self) -> None:
        if self._maybe_proxy():
            return
        self._require_body_disposition()
        self._generated_page = False
        config = self._server.config
        # Preflight must succeed without auth, or the real request never happens.
        self.send_response(HTTPStatus.NO_CONTENT)
        if config.cors:
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        if config.dav:
            from servery import _webdav

            self.send_header("DAV", _webdav.dav_class(config))
            self.send_header("MS-Author-Via", "DAV")
            self.send_header("Allow", _webdav.allow_header(config))
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

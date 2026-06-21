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

import datetime
import email.utils
import http.server
import io
import os
import shutil
import ssl
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, BinaryIO, cast

from servery import __version__, listing, ranges, security

if TYPE_CHECKING:
    from _typeshed import SupportsRead, SupportsWrite

    from servery.server import ServeryHTTPServer

_COPY_BUFSIZE = 64 * 1024
_WWW_AUTHENTICATE = 'Basic realm="servery", charset="UTF-8"'


def _copy_n(source: SupportsRead[bytes], dest: SupportsWrite[bytes], count: int) -> None:
    """Copy exactly ``count`` bytes (or until EOF) from ``source`` to ``dest``."""
    remaining = count
    while remaining > 0:
        chunk = source.read(min(_COPY_BUFSIZE, remaining))
        if not chunk:
            break
        dest.write(chunk)
        remaining -= len(chunk)


class ServeryHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP/1.1 file-serving handler with servery's safety, listing, and ranges."""

    protocol_version = "HTTP/1.1"
    server_version = f"servery/{__version__}"
    _body_remaining: int | None = None

    @property
    def _server(self) -> ServeryHTTPServer:
        return cast("ServeryHTTPServer", self.server)

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
        if not self._authorized():
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            # Directory redirect / index lookup / listing stays in the base.
            return super().send_head()
        return self._serve_file(path)

    def do_GET(self) -> None:
        f = self.send_head()
        if f is None:
            return
        try:
            self._send_body(f)
        finally:
            f.close()

    def _serve_file(self, path: str) -> BinaryIO | None:
        try:
            f = open(path, "rb")  # noqa: SIM115 (handed to the caller / closed on error)
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            stat = os.fstat(f.fileno())
            size = stat.st_size
            etag = _make_etag(stat)
            last_modified = self.date_time_string(stat.st_mtime)
            ctype = self.guess_type(path)

            if self._is_not_modified(etag, stat.st_mtime):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                f.close()
                return None

            range_header = self.headers.get("Range")
            if range_header and not self._if_range_ok(etag, stat.st_mtime):
                range_header = None
            requested = ranges.parse(range_header, size)

            if isinstance(requested, ranges._Unsatisfiable):
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
                self.send_header("Content-Range", f"bytes {requested.start}-{requested.end}/{size}")
                self.send_header("Content-Length", str(requested.length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                f.seek(requested.start)
                self._body_remaining = requested.length
                return f

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            self._body_remaining = None
            return f
        except BaseException:
            f.close()
            raise

    def _send_body(self, source: BinaryIO) -> None:
        count = self._body_remaining
        sock = self.connection
        # Zero-copy where we can: a plain (non-TLS) socket and a real file.
        if not isinstance(sock, ssl.SSLSocket):
            try:
                sock.sendfile(source, source.tell(), count)
                return
            except (OSError, ValueError):
                pass  # not a regular file (e.g. BytesIO listing) — copy in userspace
        if count is None:
            shutil.copyfileobj(source, self.wfile)
        else:
            _copy_n(source, self.wfile, count)

    # --- authentication --------------------------------------------------

    def _authorized(self) -> bool:
        credential = self._server.credential
        if credential is None:
            return True
        header = self.headers.get("Authorization")
        if header is not None and credential.check_header(header):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", _WWW_AUTHENTICATE)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # --- conditional requests -------------------------------------------

    def _is_not_modified(self, etag: str, mtime: float) -> bool:
        # If-None-Match takes precedence; If-Modified-Since is ignored when present.
        inm = self.headers.get("If-None-Match")
        if inm is not None:
            return _etag_matches(inm, etag)
        ims = self.headers.get("If-Modified-Since")
        if ims:
            return _not_modified_since(ims, mtime)
        return False

    def _if_range_ok(self, etag: str, mtime: float) -> bool:
        condition = self.headers.get("If-Range")
        if condition is None:
            return True
        condition = condition.strip()
        if condition.startswith(('"', "W/")):
            return condition == etag  # strong comparison
        return _not_modified_since(condition, mtime)

    # --- directory listing (v0.2) ---------------------------------------

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        parts = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parts.query)
        sort = listing.code_to_sort(params.get("C", ["N"])[0])
        order = "desc" if params.get("O", ["A"])[0] == "D" else "asc"
        query = params.get("q", [""])[0]
        display = urllib.parse.unquote(parts.path, errors="surrogatepass")
        try:
            body = listing.render(
                os.fspath(path),
                display,
                show_hidden=self._server.config.show_hidden,
                sort=sort,
                order=order,
                query=query,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    # --- universal response shaping -------------------------------------

    def end_headers(self) -> None:
        # nosniff on everything, including error pages: we serve arbitrary files,
        # so MIME-sniffing is a stored-XSS vector we close by default.
        self.send_header("X-Content-Type-Options", "nosniff")
        if isinstance(self.connection, ssl.SSLSocket):
            # HSTS is only meaningful (and only valid) over TLS.
            self.send_header("Strict-Transport-Security", "max-age=63072000")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (matches base signature)
        if not self._server.config.quiet:
            super().log_message(format, *args)


def _make_etag(stat: os.stat_result) -> str:
    # Strong validator from size + nanosecond mtime (same shape nginx uses): it
    # is safe for If-Range and for both weak and strong If-None-Match comparison.
    return f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'


def _etag_matches(header: str, etag: str) -> bool:
    header = header.strip()
    if header == "*":
        return True
    return any(_unweak(tag.strip()) == _unweak(etag) for tag in header.split(","))


def _unweak(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def _not_modified_since(header: str, mtime: float) -> bool:
    try:
        since = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError, OverflowError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=datetime.UTC)
    last = datetime.datetime.fromtimestamp(mtime, datetime.UTC).replace(microsecond=0)
    return last <= since

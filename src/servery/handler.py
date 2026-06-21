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

from servery import __version__, _log, archive, listing, ranges, security, upload

if TYPE_CHECKING:
    from _typeshed import SupportsRead, SupportsWrite

    from servery.server import ServeryHTTPServer

_COPY_BUFSIZE = 64 * 1024
_WWW_AUTHENTICATE = 'Basic realm="servery", charset="UTF-8"'
# CSP for servery-GENERATED pages (listing / error): no scripts, inline styles
# only, self forms. Served files are NOT given a CSP (it would break real sites).
_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'"


def _copy_n(source: SupportsRead[bytes], dest: SupportsWrite[bytes], count: int) -> None:
    """Copy exactly ``count`` bytes (or until EOF) from ``source`` to ``dest``."""
    remaining = count
    while remaining > 0:
        chunk = source.read(min(_COPY_BUFSIZE, remaining))
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
            chunk = bytes(self._buffer)
            self._wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self._buffer.clear()

    def close(self) -> None:
        self._flush()
        self._wfile.write(b"0\r\n\r\n")


class ServeryHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP/1.1 file-serving handler with servery's safety, listing, and ranges."""

    protocol_version = "HTTP/1.1"
    server_version = f"servery/{__version__}"
    index_pages = ("index.html", "index.htm")
    _body_remaining: int | None = None
    _generated_page: bool = False

    @property
    def _server(self) -> ServeryHTTPServer:
        return cast("ServeryHTTPServer", self.server)

    def setup(self) -> None:
        super().setup()
        # A default socket timeout bounds slow/idle clients (Slowloris).
        self.connection.settimeout(self._server.config.timeout)

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
        self._generated_page = False
        if not self._authorized():
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return self._serve_directory(path)
        if not os.path.exists(path) and self._server.config.spa:
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
        archive_format = urllib.parse.parse_qs(parts.query).get("archive", [""])[0]
        if archive_format in {"tar.gz", "zip"}:
            return self._serve_archive(path, archive_format)
        # Index lookup goes through the SAME containment check as everything else:
        # an index.html symlinked outside the root must not be served.
        for name in self.index_pages:
            candidate = os.path.join(path, name)
            if os.path.isfile(candidate) and security.is_contained(
                self._server.root_real, candidate
            ):
                return self._serve_file(candidate)
        return self.list_directory(path)

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
        except OSError:  # pragma: no cover - client hung up, or a file changed mid-walk
            # The chunked body is partly sent and unrecoverable; close the
            # connection so the client gets a definite end-of-message rather than
            # a truncated, terminator-less body.
            self.close_connection = True
        return

    def do_GET(self) -> None:
        f = self.send_head()
        if f is None:
            return
        try:
            self._send_body(f)
        finally:
            f.close()

    # --- upload (v0.6) ---------------------------------------------------

    def do_POST(self) -> None:
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
        if length > config.max_upload_size:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload exceeds the size limit")
            return

        reader = upload.BoundedReader(self.rfile, length)
        try:
            upload.save(reader, boundary, dest_dir, allow_overwrite=config.allow_overwrite)
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
            cache_control = self._server.config.cache_control

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
                self.send_header("Cache-Control", cache_control)
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
            self.send_header("Cache-Control", cache_control)
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
        if count == 0:
            return  # socket.sendfile treats count==0 as "whole file"; never that
        sock = self.connection
        # Zero-copy fast path for plain sockets. (socket.sendfile transparently
        # handles non-regular sources like BytesIO via its own send loop; TLS
        # sockets cannot sendfile, so they take the userspace path below.)
        if not isinstance(sock, ssl.SSLSocket):
            before = source.tell()
            try:
                sock.sendfile(source, before, count)
                return
            except (OSError, ValueError):
                # If bytes were already sent the stream is broken — re-raise
                # rather than resend (which would overrun a range). Only retry in
                # userspace when nothing went out.
                if source.tell() != before:
                    raise
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
        self._generated_page = True
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
                upload=self._server.config.upload,
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
        super().end_headers()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._generated_page = True  # the error body is generated HTML
        super().send_error(code, message, explain)

    def do_OPTIONS(self) -> None:
        self._generated_page = False
        # Preflight must succeed without auth, or the real request never happens.
        self.send_response(HTTPStatus.NO_CONTENT)
        if self._server.config.cors:
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base signature)
        # Route through the logging module instead of writing to stderr directly.
        _log.logger.info("%s %s", self.address_string(), format % args)


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
        if since.tzinfo is None:
            since = since.replace(tzinfo=datetime.UTC)
        # A corrupt/extreme on-disk mtime must not crash the conditional path.
        last = datetime.datetime.fromtimestamp(mtime, datetime.UTC).replace(microsecond=0)
    except (TypeError, ValueError, IndexError, OverflowError, OSError):
        return False
    return last <= since

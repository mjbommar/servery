"""WSGI (PEP 3333) hosting — opt-in via ``--wsgi module:app``.

WSGI is synchronous, so it maps directly onto servery's thread-per-connection
model. Rather than the stdlib ``wsgiref`` server engine (which is HTTP/1.0 and
closes the connection after every response), this is a lean engine wired into
servery's HTTP/1.1 handler: it keeps connections alive, sets ``Content-Length``
when the app does, and falls back to chunked transfer-encoding otherwise.

Compliance is checked in the tests with ``wsgiref.validate.validator`` — the
official PEP 3333 validator — so the engine stays honest without shipping the
slow reference server.
"""

from __future__ import annotations

import contextlib
import io
import ssl
import sys
import urllib.parse
from typing import Any

from servery import _appspec, _http1, _log
from servery.handler import ServeryHandler, _ChunkedWriter


def load_app(spec: str) -> Any:
    """Import a WSGI app from ``"module:attribute"`` (attr defaults to ``application``)."""
    return _appspec.load_app(spec, default_attr="application", label="--wsgi")


class _BodyReader:
    """A read-bounded view of the request body for ``wsgi.input``.

    Reads never run past ``Content-Length`` into the next pipelined request.
    Implements just what PEP 3333 requires of ``wsgi.input``.
    """

    def __init__(self, source: io.BufferedIOBase, length: int) -> None:
        self._source = source
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size < 0 else min(size, self._remaining)
        data = self._source.read(want)
        self._remaining -= len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        limit = self._remaining if size < 0 else min(size, self._remaining)
        data = self._source.readline(limit)
        self._remaining -= len(data)
        return data

    def readlines(self, hint: int = -1) -> list[bytes]:
        return list(iter(self.readline, b""))

    def __iter__(self) -> Any:
        return iter(self.readline, b"")


def build_environ(handler: ServeryHandler) -> dict[str, Any]:
    """Build a PEP 3333 ``environ`` from servery's parsed request."""
    path, _, query = handler.path.partition("?")
    headers = handler.headers
    length = int(headers.get("content-length") or 0)
    server_host, server_port = handler.server.server_address[:2]  # ty: ignore[not-subscriptable]
    environ: dict[str, Any] = {
        "REQUEST_METHOD": handler.command,
        "SCRIPT_NAME": "",
        "PATH_INFO": urllib.parse.unquote(path, "iso-8859-1"),
        "QUERY_STRING": query,
        "SERVER_NAME": str(server_host),
        "SERVER_PORT": str(server_port),
        "SERVER_PROTOCOL": handler.request_version or "HTTP/1.1",
        "REMOTE_ADDR": handler.client_address[0],
        "REMOTE_PORT": str(handler.client_address[1]),
        "CONTENT_TYPE": headers.get("content-type", ""),
        "CONTENT_LENGTH": str(length) if length else "",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https" if isinstance(handler.connection, ssl.SSLSocket) else "http",
        "wsgi.input": _BodyReader(handler.rfile, length),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    for name, value in headers.items():
        key = "HTTP_" + name.upper().replace("-", "_")
        if key in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH"):
            continue
        environ[key] = f"{environ[key]},{value}" if key in environ else value
    return environ


def run(handler: ServeryHandler, app: Any) -> None:
    """Run one request through the WSGI ``app`` and write the HTTP/1.1 response."""
    _Exchange(handler, app).run()


class _Exchange:
    __slots__ = ("_app", "_chunked", "_handler", "_headers", "_status", "_writer")

    def __init__(self, handler: ServeryHandler, app: Any) -> None:
        self._handler = handler
        self._app = app
        self._status: str | None = None
        self._headers: list[tuple[str, str]] = []
        self._writer: Any = None  # set once headers are flushed
        self._chunked = False

    def _start_response(self, status: str, headers: list[tuple[str, str]], exc_info: Any = None):
        if exc_info:
            try:
                if self._writer is not None:  # already committed — must re-raise
                    raise exc_info[1].with_traceback(exc_info[2])
            finally:
                exc_info = None
        elif self._status is not None:
            raise AssertionError("start_response() called twice without exc_info")
        self._status = status
        self._headers = list(headers)
        return self._write

    def _header_lines(self) -> tuple[list[str], set[str]]:
        h = self._handler
        status = self._status or ""  # always set by start_response before we get here
        present = {name.lower() for name, _ in self._headers}
        lines = [f"{h.protocol_version} {status}"]
        lines += [f"{name}: {value}" for name, value in self._headers]
        if "server" not in present:
            lines.append(f"Server: {h.version_string()}")
        if "date" not in present:
            lines.append(f"Date: {h.date_time_string()}")
        return lines, present

    def _send_materialized(self, body: bytes) -> None:
        # Common case: the app returned a list/tuple, so the whole body is known.
        # Set Content-Length and write head + body in ONE socket write (no chunked).
        h = self._handler
        lines, present = self._header_lines()
        if "content-length" not in present:
            lines.append(f"Content-Length: {len(body)}")
        if h.close_connection:
            lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        h.wfile.write(head if h.command == "HEAD" else head + body)

    def _flush_headers(self) -> None:
        h = self._handler
        lines, present = self._header_lines()
        if "content-length" in present:
            pass  # length known -> keep-alive as the request allows
        elif h.protocol_version >= "HTTP/1.1" and not h.close_connection and h.command != "HEAD":
            self._chunked = True
            lines.append("Transfer-Encoding: chunked")
        else:
            h.close_connection = True
        if h.close_connection:
            lines.append("Connection: close")
        h.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        self._writer = _ChunkedWriter(h.wfile) if self._chunked else h.wfile

    def _write(self, data: bytes) -> None:
        if self._writer is None:
            self._flush_headers()
        if data and self._handler.command != "HEAD":
            self._writer.write(data)

    def run(self) -> None:
        h = self._handler
        try:
            result = self._app(build_environ(h), self._start_response)
            try:
                if isinstance(result, (list, tuple)):
                    self._send_materialized(b"".join(result))
                else:  # a generator / streaming iterable
                    for data in result:
                        if data:
                            self._write(data)
                    if self._writer is None:
                        self._write(b"")  # commit headers for an empty body
                    if self._chunked:
                        self._writer.close()
            finally:
                close = getattr(result, "close", None)
                if close is not None:
                    close()
        except Exception:
            # The app (or its iterator) raised. Log with traceback; send a 500 if
            # nothing was committed yet, otherwise just close the connection.
            _log.logger.error('WSGI app error: "%s %s"', h.command, h.path, exc_info=True)
            if self._writer is None:
                with contextlib.suppress(OSError):
                    h.wfile.write(_http1.INTERNAL_ERROR)
            h.close_connection = True
            return
        h.log_request(self._status.split(" ", 1)[0] if self._status else "-")


class WSGIHandler(ServeryHandler):
    """Routes every method and path to the WSGI app (no file serving)."""

    def handle(self) -> None:
        # WSGI is HTTP/1.1 only — skip ServeryHandler's HTTP/2 dispatch.
        super(ServeryHandler, self).handle()

    def _run_wsgi(self) -> None:
        run(self, self._server.wsgi_app)

    def do_GET(self) -> None:
        self._run_wsgi()

    def do_HEAD(self) -> None:
        self._run_wsgi()

    def do_POST(self) -> None:
        self._run_wsgi()

    def do_PUT(self) -> None:
        self._run_wsgi()

    def do_DELETE(self) -> None:
        self._run_wsgi()

    def do_PATCH(self) -> None:
        self._run_wsgi()

    def do_OPTIONS(self) -> None:
        self._run_wsgi()

"""The servery request handler.

We subclass the stdlib ``SimpleHTTPRequestHandler`` rather than reimplementing
HTTP: the base already gives us correct request parsing, HEAD/GET, directory
redirects, ``If-Modified-Since``, and MIME typing. servery overrides only what
it improves:

* ``translate_path`` — routes every path through the :mod:`servery.security`
  containment check (closing the symlink-escape gap);
* ``list_directory`` — renders the rich listing from :mod:`servery.listing`;
* ``end_headers`` — injects ``X-Content-Type-Options: nosniff`` on every response;
* ``protocol_version`` — HTTP/1.1 (persistent connections) instead of HTTP/1.0;
* ``log_message`` — honors ``--quiet``.
"""

from __future__ import annotations

import http.server
import io
import os
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from servery import __version__, listing, security

if TYPE_CHECKING:
    from servery.server import ServeryHTTPServer


class ServeryHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP/1.1 file-serving handler with servery's safety and listing."""

    protocol_version = "HTTP/1.1"
    server_version = f"servery/{__version__}"

    @property
    def _server(self) -> ServeryHTTPServer:
        return cast("ServeryHTTPServer", self.server)

    def translate_path(self, path: str) -> str:
        fs_path = super().translate_path(path)
        # Fail closed: a path that escapes the root (e.g. via a symlink) maps to
        # the empty string, which the base send_head turns into a 404.
        if security.is_contained(self._server.root_real, fs_path):
            return fs_path
        return ""

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        display = urllib.parse.unquote(
            self.path.split("?", 1)[0].split("#", 1)[0],
            errors="surrogatepass",
        )
        try:
            body = listing.render(
                os.fspath(path),
                display,
                show_hidden=self._server.config.show_hidden,
            )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def end_headers(self) -> None:
        # nosniff on everything, including error pages: we hand out arbitrary
        # files, so MIME-sniffing is a stored-XSS vector we close by default.
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (matches base signature)
        if not self._server.config.quiet:
            super().log_message(format, *args)

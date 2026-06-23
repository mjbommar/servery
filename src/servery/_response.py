"""Shared buffered-response building for the HTTP/2 and HTTP/3 backends.

The HTTP/1.1 handler streams files and does ranges/conditionals, so it keeps its
own path. The buffered backends (HTTP/2, HTTP/3) resolve a request to an in-memory
``(status, headers, body)`` triple through the *same* helpers here, so the gzip
content-coding decision, the security/cache headers, and the directory-listing
response can never drift between transports (they did before this was lifted).

Headers are wire form: ``list[(bytes, bytes)]`` with lowercase names.
"""

from __future__ import annotations

import mimetypes
import os
from typing import TYPE_CHECKING

from servery import _compress, listing
from servery.handler import _CSP

if TYPE_CHECKING:
    from servery.config import Config

_HeaderList = list[tuple[bytes, bytes]]
_LISTING_TYPE = "text/html; charset=utf-8"


def guess_type(fs_path: str) -> str:
    """MIME type for ``fs_path`` with the octet-stream fallback (one source of truth)."""
    return mimetypes.guess_file_type(fs_path)[0] or "application/octet-stream"


def base_headers(config: Config, *, tls: bool) -> _HeaderList:
    """The per-response policy headers: nosniff, HSTS (TLS only), CORS, Cache-Control."""
    headers: _HeaderList = []
    if config.security_headers:
        headers.append((b"x-content-type-options", b"nosniff"))
        if tls:
            headers.append((b"strict-transport-security", b"max-age=63072000"))
    if config.cors:
        headers.append((b"access-control-allow-origin", b"*"))
    headers.append((b"cache-control", config.cache_control.encode("latin-1")))
    return headers


def error(status: int) -> tuple[int, _HeaderList, bytes]:
    """A minimal text/plain error triple (shared 404/405/… for the buffered backends)."""
    body = str(status).encode("ascii")
    headers: _HeaderList = [
        (b"content-type", b"text/plain"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    return status, headers, body


def finalize_body(
    config: Config, headers: _HeaderList, ctype: str, body: bytes, accept_encoding: str
) -> tuple[int, _HeaderList, bytes]:
    """Append content-type/length, gzip when accepted, Vary on compressible types.

    The single content-coding decision (RFC 9110): compressible type → ``Vary:
    Accept-Encoding``; and gzip only when enabled, in the size band, and the client
    accepts it. Shared by every buffered backend so the decision stays identical.
    """
    if _compress.compressible(ctype):
        headers.append((b"vary", b"accept-encoding"))
        if (
            config.compress
            and _compress.GZIP_MIN <= len(body) <= _compress.GZIP_MAX
            and _compress.accepts_gzip(accept_encoding)
        ):
            body = _compress.gzip_bytes(body)
            headers.append((b"content-encoding", b"gzip"))
    headers.append((b"content-type", ctype.encode("latin-1")))
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    return 200, headers, body


def build_static(
    config: Config, fs_path: str, display: str, accept_encoding: str, *, tls: bool
) -> tuple[int, _HeaderList, bytes]:
    """Resolve an already-contained path to a buffered (status, headers, body).

    ``fs_path`` must already have passed the transport's containment check (an empty
    string means "escaped" → 404). ``display`` is the URL path (for the redirect and
    the listing heading). The dir-or-file logic shared by HTTP/2 and HTTP/3.
    """
    headers = base_headers(config, tls=tls)
    if not fs_path:
        return error(404)
    if os.path.isdir(fs_path):  # noqa: PTH112 - os-level by design (shared with the handler)
        if not display.endswith("/"):
            return 301, [(b"location", (display + "/").encode("latin-1"))], b""
        try:
            body = listing.render(
                fs_path, display, show_hidden=config.show_hidden, per_page=listing.DEFAULT_PAGE_SIZE
            )
        except OSError:
            return error(404)
        if config.security_headers:
            # The listing's own inline styles need the full CSP (style-src etc.);
            # "default-src 'none'" alone renders it unstyled.
            headers.append((b"content-security-policy", _CSP.encode("latin-1")))
            headers.append((b"referrer-policy", b"no-referrer"))
        return finalize_body(config, headers, _LISTING_TYPE, body, accept_encoding)
    try:
        with open(fs_path, "rb") as handle:  # noqa: PTH123 - os-level by design
            body = handle.read()
    except OSError:
        return error(404)
    return finalize_body(config, headers, guess_type(fs_path), body, accept_encoding)

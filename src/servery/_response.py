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

from servery import _compress, _conditional, _http1, listing
from servery.handler import _CSP

if TYPE_CHECKING:
    from servery.config import Config

_HeaderList = list[tuple[bytes, bytes]]
_LISTING_TYPE = "text/html; charset=utf-8"


def guess_type(fs_path: str) -> str:
    """MIME type for ``fs_path`` (octet-stream fallback), with UTF-8 charset on text.

    One source of truth for the buffered backends; the charset keeps browsers from
    mis-decoding a UTF-8 text file (e.g. Markdown) that declares no in-band encoding.
    """
    return _compress.with_charset(mimetypes.guess_file_type(fs_path)[0] or "application/octet-stream")


def base_headers(config: Config, *, tls: bool) -> _HeaderList:
    """The per-response policy headers: nosniff, HSTS (TLS only), CORS, Cache-Control.

    The cross-cutting trio comes from the single source of truth, ``_http1.policy_headers``
    (shared with WSGI/CGI/ASGI/proxy), encoded to wire bytes; Cache-Control is added
    here because the buffered backends serve files, not arbitrary apps.
    """
    # h2/h3 require lowercase field names (RFC 9113 §8.2.1); policy_headers returns
    # the canonical Title-Case used on the HTTP/1.1 wire, so lowercase here.
    headers: _HeaderList = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in _http1.policy_headers(
            security_headers=config.security_headers, cors=config.cors, tls=tls
        )
    ]
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
    headers: _HeaderList, ctype: str, body: bytes, *, gzip: bool
) -> tuple[int, _HeaderList, bytes]:
    """Append Vary (compressible), Content-Encoding (when ``gzip``), Content-Type/Length.

    The gzip *decision* is made by the caller via :func:`_compress.should_gzip` (so it
    can agree with the ETag variant); this just assembles the body + headers.
    """
    if _compress.compressible(ctype):
        headers.append((b"vary", b"accept-encoding"))
    if gzip:
        body = _compress.gzip_bytes(body)
        headers.append((b"content-encoding", b"gzip"))
    headers.append((b"content-type", ctype.encode("latin-1")))
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    return 200, headers, body


def build_static(
    config: Config,
    fs_path: str,
    display: str,
    accept_encoding: str,
    *,
    tls: bool,
    if_none_match: str | None = None,
    if_modified_since: str | None = None,
) -> tuple[int, _HeaderList, bytes]:
    """Resolve an already-contained path to a buffered (status, headers, body).

    ``fs_path`` must already have passed the transport's containment check (an empty
    string means "escaped" → 404). ``display`` is the URL path (for the redirect and
    the listing heading). Files get a strong ETag + Last-Modified and honor
    ``If-None-Match`` / ``If-Modified-Since`` (304) — the same validators and
    conditional semantics as the HTTP/1.1 handler, via :mod:`servery._conditional`.
    The dir-or-file logic shared by HTTP/2 and HTTP/3.
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
        gzip = _compress.should_gzip(
            _LISTING_TYPE, len(body), accept_encoding, enabled=config.compress
        )
        return finalize_body(headers, _LISTING_TYPE, body, gzip=gzip)
    try:
        stat = os.stat(fs_path)  # noqa: PTH116 - os-level by design
    except OSError:
        return error(404)
    ctype = guess_type(fs_path)
    # Decide gzip from the identity size (no read needed) so the ETag for the
    # representation the client would get is known before any conditional check.
    gzip = _compress.should_gzip(ctype, stat.st_size, accept_encoding, enabled=config.compress)
    etag = _conditional.make_etag(stat)
    if gzip:
        etag = _conditional.gzip_variant(etag)
    last_modified = _http1.format_http_date(stat.st_mtime)
    headers.append((b"etag", etag.encode("ascii")))
    headers.append((b"last-modified", last_modified.encode("latin-1")))
    if _conditional.is_not_modified(
        etag, stat.st_mtime, if_none_match=if_none_match, if_modified_since=if_modified_since
    ):
        return 304, headers, b""  # revalidated — no body, no file read
    try:
        with open(fs_path, "rb") as handle:  # noqa: PTH123 - os-level by design
            body = handle.read()
    except OSError:
        return error(404)
    return finalize_body(headers, ctype, body, gzip=gzip)

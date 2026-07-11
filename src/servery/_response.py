"""Shared response planning for the HTTP/2 and HTTP/3 backends.

The HTTP/1.1 handler streams files and does ranges/conditionals, so it keeps its
own path. HTTP/2 and HTTP/3 resolve a request to a ``(status, headers, body)``
triple through the *same* helpers here. Small bodies are bytes; large identity
files transfer an owned open handle, so validators and bytes describe one file
even if its path is atomically replaced after planning.

Headers are wire form: ``list[(bytes, bytes)]`` with lowercase names.
"""

from __future__ import annotations

import mimetypes
import os
from typing import TYPE_CHECKING

from servery import _compress, _conditional, _http1, _static, listing
from servery._static import FileBody

if TYPE_CHECKING:
    from servery.config import Config

_HeaderList = list[tuple[bytes, bytes]]
_LISTING_TYPE = "text/html; charset=utf-8"


type ResponseBody = bytes | FileBody


def guess_type(fs_path: str) -> str:
    """MIME type for ``fs_path`` (octet-stream fallback), with UTF-8 charset on text.

    One source of truth for the buffered backends; the charset keeps browsers from
    mis-decoding a UTF-8 text file (e.g. Markdown) that declares no in-band encoding.
    """
    return _compress.with_charset(
        mimetypes.guess_file_type(fs_path)[0] or "application/octet-stream"
    )


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
    headers: _HeaderList,
    ctype: str,
    body: bytes,
    *,
    coding: str | None,
    already_encoded: bool = False,
) -> tuple[int, _HeaderList, bytes]:
    """Append Vary (compressible), Content-Encoding (when coded), Content-Type/Length.

    The coding *decision* is made by the caller via :func:`_compress.choose_encoding`
    (so it can agree with the ETag variant); this just assembles the body + headers.
    """
    if _compress.compressible(ctype):
        headers.append((b"vary", b"accept-encoding"))
    if coding is not None:
        if not already_encoded:
            body = _compress.encode(body, coding)
        headers.append((b"content-encoding", coding.encode("ascii")))
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
    compression_cache: _compress.CompressionCache | None = None,
) -> tuple[int, _HeaderList, ResponseBody]:
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
                fs_path,
                display,
                show_hidden=config.show_hidden,
                per_page=config.listing_page_size,
                max_entries=config.max_listing_entries,
                details_threshold=config.listing_details_threshold,
            )
        except OSError:
            return error(404)
        if config.security_headers:
            # The listing's own inline styles need the full CSP (style-src etc.);
            # "default-src 'none'" alone renders it unstyled.
            headers.append((b"content-security-policy", _static.GENERATED_CSP.encode("latin-1")))
            headers.append((b"referrer-policy", b"no-referrer"))
        coding = _compress.negotiate(accept_encoding, enabled=config.compress)
        return finalize_body(headers, _LISTING_TYPE, body, coding=coding)
    ctype = guess_type(fs_path)
    try:
        opened = _static.open_file(
            fs_path,
            ctype,
            accept_encoding,
            compression_enabled=config.compress,
            max_compress_size=min(config.max_compress_size, config.max_buffered_response),
        )
    except OSError:
        return error(404)
    try:
        stat = opened.stat
        coding = opened.coding
        headers.append((b"etag", opened.etag.encode("ascii")))
        headers.append((b"last-modified", opened.last_modified.encode("latin-1")))
        if _conditional.is_not_modified(
            opened.etag,
            stat.st_mtime,
            if_none_match=if_none_match,
            if_modified_since=if_modified_since,
        ):
            return 304, headers, b""  # revalidated — no body read
        if stat.st_size > config.max_buffered_response:
            if _compress.compressible(ctype):
                headers.append((b"vary", b"accept-encoding"))
            headers.append((b"content-type", ctype.encode("latin-1")))
            headers.append((b"content-length", str(stat.st_size).encode("ascii")))
            body = opened
            opened = None
            return 200, headers, body
        if coding is not None and compression_cache is not None:
            key = _compress.cache_key(fs_path, stat, coding)

            def encode_file() -> bytes:
                return _compress.encode(opened.handle.read(), coding)

            body_bytes = compression_cache.get_or_compute(key, encode_file)
            return finalize_body(headers, ctype, body_bytes, coding=coding, already_encoded=True)
        body_bytes = opened.handle.read()
        return finalize_body(headers, ctype, body_bytes, coding=coding)
    except OSError:
        return error(404)
    finally:
        if opened is not None:
            opened.close()

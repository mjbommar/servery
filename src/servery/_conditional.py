"""Shared HTTP validators + conditional-request logic (RFC 9110 §8.8, §13).

The ETag shape and the If-None-Match / If-Modified-Since semantics are used by the
HTTP/1.1 handler (full streaming path) and the buffered HTTP/2 / HTTP/3 backends, so
they live here as transport-agnostic pure functions — pass the request header values
in, get a verdict out — and can't drift between transports.
"""

from __future__ import annotations

import datetime
import email.utils
import os


def make_etag(stat: os.stat_result) -> str:
    """A strong validator from size + nanosecond mtime (the shape nginx uses).

    Safe for If-Range and for both weak and strong If-None-Match comparison.
    """
    return f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'


def gzip_variant(etag: str) -> str:
    """The distinct ETag for the gzip-encoded representation (RFC 9110 §8.8.3.3)."""
    return etag[:-1] + '-gz"'


#: Per-coding ETag suffix — a distinct strong validator per representation.
_CODING_SUFFIX = {"gzip": "-gz", "zstd": "-zst"}


def coding_variant(etag: str, coding: str | None) -> str:
    """The distinct ETag for a content-coded representation (RFC 9110 §8.8.3.3).

    ``coding`` is ``"gzip"``, ``"zstd"``, or ``None`` (identity → the tag unchanged).
    """
    if coding is None:
        return etag
    return etag[:-1] + _CODING_SUFFIX[coding] + '"'


def _unweak(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def etag_matches(header: str, etag: str) -> bool:
    """True if an If-None-Match / If-Match header value matches ``etag`` (or is ``*``)."""
    header = header.strip()
    if header == "*":
        return True
    return any(_unweak(tag.strip()) == _unweak(etag) for tag in header.split(","))


def not_modified_since(header: str, mtime: float) -> bool:
    """True if ``mtime`` is at or before the HTTP-date in an If-Modified-Since/If-Range."""
    try:
        since = email.utils.parsedate_to_datetime(header)
        if since.tzinfo is None:
            since = since.replace(tzinfo=datetime.UTC)
        # A corrupt/extreme on-disk mtime must not crash the conditional path.
        last = datetime.datetime.fromtimestamp(mtime, datetime.UTC).replace(microsecond=0)
    except (TypeError, ValueError, IndexError, OverflowError, OSError):
        return False
    return last <= since


def is_not_modified(
    etag: str, mtime: float, *, if_none_match: str | None, if_modified_since: str | None
) -> bool:
    """Resolve a conditional GET to "send 304?" — If-None-Match wins over -Modified-Since."""
    if if_none_match is not None:
        return etag_matches(if_none_match, etag)
    if if_modified_since:
        return not_modified_since(if_modified_since, mtime)
    return False

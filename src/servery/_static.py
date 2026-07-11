"""Transport-neutral acquisition and representation facts for static files."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import BinaryIO

from servery import _compress, _conditional, _http1, ranges, security

# CSP for servery-generated pages (listing / error): no scripts, inline styles
# only, self forms. Served files are not given a CSP because it would break sites.
GENERATED_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'self'"
)


@dataclass(slots=True)
class FileBody:
    """One opened file identity plus the representation facts derived from it."""

    path: str
    handle: BinaryIO = field(repr=False, compare=False)
    stat: os.stat_result = field(repr=False, compare=False)
    ctype: str
    coding: str | None
    etag: str
    last_modified: str

    @property
    def size(self) -> int:
        return self.stat.st_size

    def close(self) -> None:
        self.handle.close()


@dataclass(frozen=True, slots=True)
class IdentitySelection:
    """Transport-neutral status and byte slice for one identity representation."""

    status: int
    offset: int
    count: int
    content_range: str | None = None


def download_requested(target: str) -> bool:
    """Whether ``target`` asks for attachment disposition via its query string."""
    return "download" in target and "download" in urllib.parse.parse_qs(
        urllib.parse.urlsplit(target).query
    )


def content_disposition(filename: str) -> str:
    """Build a safe attachment field value with ASCII and RFC 8187 names."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    ascii_name = "".join(char for char in ascii_name if char.isprintable()).replace('"', "")
    extended = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{extended}"


def directory_redirect(target: str) -> str | None:
    """Return the slash-appending redirect target, or ``None`` when already canonical."""
    parts = urllib.parse.urlsplit(target)
    if parts.path.endswith(("/", "%2f", "%2F")):
        return None
    return urllib.parse.urlunsplit(parts._replace(path=parts.path + "/"))


def find_contained_index(
    root_real: str,
    directory: str,
    names: tuple[str, ...],
) -> str | None:
    """Return the first regular index that still resolves inside ``root_real``."""
    for name in names:
        candidate = os.path.join(directory, name)  # noqa: PTH118 - OS-level path contract
        if os.path.isfile(candidate) and security.is_contained(  # noqa: PTH113
            root_real, candidate
        ):
            return candidate
    return None


def select_identity(
    opened: FileBody,
    *,
    range_header: str | None,
    if_range: str | None,
    if_none_match: str | None,
    if_modified_since: str | None,
) -> IdentitySelection:
    """Resolve validators and a single byte range against one opened identity."""
    size = opened.size
    if _conditional.is_not_modified(
        opened.etag,
        opened.stat.st_mtime,
        if_none_match=if_none_match,
        if_modified_since=if_modified_since,
    ):
        return IdentitySelection(304, 0, 0)
    if not _conditional.if_range_matches(
        if_range,
        opened.etag,
        opened.stat.st_mtime,
    ):
        range_header = None
    requested = ranges.parse(range_header, size)
    if requested is ranges.UNSATISFIABLE:
        return IdentitySelection(416, 0, 0, f"bytes */{size}")
    if isinstance(requested, ranges.ByteRange):
        return IdentitySelection(
            206,
            requested.start,
            requested.length,
            f"bytes {requested.start}-{requested.end}/{size}",
        )
    return IdentitySelection(200, 0, size)


def open_file(
    path: str,
    ctype: str,
    accept_encoding: str,
    *,
    compression_enabled: bool,
    max_compress_size: int,
    allow_compression: bool = True,
) -> FileBody:
    """Open ``path`` and derive all metadata from that one file identity."""
    handle = open(path, "rb")  # noqa: PTH123, SIM115 - ownership transfers on success
    try:
        stat = os.fstat(handle.fileno())
        coding = (
            _compress.choose_encoding(
                ctype,
                stat.st_size,
                accept_encoding,
                enabled=compression_enabled,
                max_size=max_compress_size,
            )
            if allow_compression
            else None
        )
        return FileBody(
            path,
            handle,
            stat,
            ctype,
            coding,
            _conditional.coding_variant(_conditional.make_etag(stat), coding),
            _http1.format_http_date(stat.st_mtime),
        )
    except BaseException:
        handle.close()
        raise

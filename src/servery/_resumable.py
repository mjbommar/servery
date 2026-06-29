"""Resumable file upload over HTTP ``PUT`` with ``Content-Range``.

The download side has been resumable since v0.3 (``Range`` / ``206``); this is the
upload counterpart. It follows the widely-deployed Google/S3 convention — the one
shape that works from a bare ``curl`` with two headers and needs no client library:

* ``PUT /path`` with no ``Content-Range`` writes the whole body (create/replace).
* ``PUT /path`` with ``Content-Range: bytes <start>-<end>/<total>`` appends the
  chunk at ``start``. While more is expected the server replies ``308`` with
  ``Range: bytes=0-<last>`` (the bytes durably stored); the final chunk that
  reaches ``total`` commits the file and returns ``201``/``200``.
* ``PUT /path`` with ``Content-Range: bytes */<total>`` and an empty body *queries*
  progress — the ``308``/``Range`` reply says how many bytes to resume from.

Chunks accumulate in a hidden ``.<name>.servery-part`` sidecar next to the target
and are committed with an atomic :func:`os.replace`, so a half-finished upload is
never visible at the destination. Chunks must arrive contiguously (each ``start``
equal to the bytes already stored); a gap is a ``409`` so the client re-queries.

This module is pure parsing + file ops (no HTTP), unit-testable without a server;
the handler maps its results to status codes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from typing import Protocol

_CHUNK = 64 * 1024
PART_SUFFIX = ".servery-part"


class ResumableError(Exception):
    """A malformed ``Content-Range`` (the handler maps this to ``400``)."""


class _Reader(Protocol):
    def read(self, size: int, /) -> bytes: ...


@dataclasses.dataclass(frozen=True, slots=True)
class ContentRange:
    """A parsed ``Content-Range`` request header.

    ``start``/``end`` are ``None`` for the ``bytes */total`` progress-query form.
    ``total`` is ``None`` for an unknown total (``bytes start-end/*``).
    """

    start: int | None
    end: int | None
    total: int | None

    @property
    def is_query(self) -> bool:
        """True for the ``bytes */total`` form (no payload, just asks the offset)."""
        return self.start is None

    @property
    def length(self) -> int:
        """The byte count of a data chunk (only valid when not :attr:`is_query`)."""
        if self.start is None or self.end is None:
            raise ValueError("length is undefined for a query (bytes */total) range")
        return self.end - self.start + 1


def _nonneg_int(text: str, label: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise ResumableError(f"Content-Range {label} is not an integer") from None
    if value < 0:
        raise ResumableError(f"Content-Range {label} is negative")
    return value


def parse_content_range(value: str) -> ContentRange:
    """Parse a request ``Content-Range`` value (RFC 9110 §14.4 grammar), else raise."""
    value = value.strip()
    if not value.startswith("bytes "):
        raise ResumableError("Content-Range must use the bytes unit")
    range_part, sep, total_part = value[len("bytes ") :].strip().partition("/")
    if not sep:
        raise ResumableError("Content-Range must be 'bytes range/total'")
    range_part, total_part = range_part.strip(), total_part.strip()
    total = None if total_part == "*" else _nonneg_int(total_part, "total")
    if range_part == "*":
        if total is None:
            raise ResumableError("Content-Range 'bytes */*' is meaningless")
        return ContentRange(None, None, total)
    start_text, dash, end_text = range_part.partition("-")
    if not dash:
        raise ResumableError("Content-Range range must be 'start-end'")
    start = _nonneg_int(start_text, "start")
    end = _nonneg_int(end_text, "end")
    if start > end:
        raise ResumableError("Content-Range start is after end")
    if total is not None and end >= total:
        raise ResumableError("Content-Range end is beyond total")
    return ContentRange(start, end, total)


def part_path(target: str) -> str:
    """The hidden sidecar that accumulates an in-progress upload for ``target``.

    A leading dot keeps it out of default listings; it lives in the target's own
    directory so the final :func:`os.replace` is an atomic same-filesystem rename.
    """
    directory, name = os.path.split(target)
    return os.path.join(directory, f".{name}{PART_SUFFIX}")


def stored_bytes(part: str) -> int:
    """How many bytes are already in the sidecar (0 if it does not exist)."""
    try:
        return os.path.getsize(part)
    except OSError:
        return 0


def append(part: str, reader: _Reader, length: int) -> int:
    """Append exactly ``length`` bytes (or until EOF) from ``reader`` to ``part``."""
    written = 0
    with open(part, "ab") as handle:
        while written < length:
            chunk = reader.read(min(_CHUNK, length - written))
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
    return written


def write_whole(target: str, reader: _Reader, length: int) -> int:
    """Stream a non-ranged PUT body to ``target`` atomically; return bytes written."""
    part = part_path(target)
    discard(part)
    written = append(part, reader, length)
    os.replace(part, target)
    return written


def commit(part: str, target: str) -> None:
    """Atomically move a completed sidecar onto the final target path."""
    os.replace(part, target)


def discard(part: str) -> None:
    """Remove a sidecar, ignoring its absence."""
    with contextlib.suppress(OSError):
        os.unlink(part)

"""HTTP Range request parsing (RFC 9110 §14.1-14.4).

servery implements byte ranges itself — the stdlib base always returns the full
body. We support a single range per request (the common case for resumable
downloads and media seeking); a multi-range request is served in full, which the
spec explicitly permits.
"""

from __future__ import annotations

import dataclasses

_PREFIX = "bytes="


def _digits(text: str) -> bool:
    """True for a non-empty run of ASCII digits (int() would accept more)."""
    return bool(text) and all(char in "0123456789" for char in text)


@dataclasses.dataclass(frozen=True, slots=True)
class ByteRange:
    """A satisfiable byte range: ``length`` bytes starting at ``start``."""

    start: int
    length: int

    @property
    def end(self) -> int:
        """The inclusive end offset, for ``Content-Range``."""
        return self.start + self.length - 1


class _Unsatisfiable:
    """Sentinel: the range cannot be satisfied (→ 416)."""


UNSATISFIABLE = _Unsatisfiable()


def parse(header: str | None, size: int) -> ByteRange | _Unsatisfiable | None:
    """Parse a ``Range`` header against a resource of ``size`` bytes.

    Returns:
        * ``None`` — no range, or one to ignore (serve the full 200 response):
          missing/blank header, non-bytes unit, multiple ranges, or malformed.
        * :class:`ByteRange` — a satisfiable single range (serve 206).
        * :data:`UNSATISFIABLE` — a syntactically valid but unsatisfiable range
          (serve 416 with ``Content-Range: bytes */size``).
    """
    if not header:
        return None
    header = header.strip()
    if not header.startswith(_PREFIX):
        return None
    spec = header[len(_PREFIX) :].strip()
    if "," in spec or "-" not in spec:
        return None

    start_text, _, end_text = spec.partition("-")

    if start_text == "":
        # Suffix range: "-N" → final N bytes.
        if not _digits(end_text):
            return None
        suffix = int(end_text)
        if suffix <= 0:
            return None
        if size == 0:
            return UNSATISFIABLE
        if suffix >= size:
            return ByteRange(0, size)
        return ByteRange(size - suffix, suffix)

    if not _digits(start_text):
        return None
    start = int(start_text)
    if start >= size:
        return UNSATISFIABLE
    if end_text == "":
        end = size - 1
    else:
        if not _digits(end_text):
            return None
        end = int(end_text)
        if end < start:
            return None
        end = min(end, size - 1)
    return ByteRange(start, end - start + 1)

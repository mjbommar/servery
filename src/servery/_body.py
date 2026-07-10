"""Shared HTTP request-body framing and bounded-reader primitives.

HTTP/1 adapters must agree on the declared body length and on whether a
connection is reusable.  This module contains the non-configurable framing rules;
callers supply their configurable size limit and decide how to report the error.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol


class Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class FramingError(ValueError):
    """A request body is ambiguously or invalidly framed."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class BodyPlan:
    """Validated HTTP/1 request-body framing."""

    length: int | None
    chunked: bool = False


def parse_framing(
    content_lengths: list[str],
    transfer_encodings: list[str],
    *,
    max_size: int | None = None,
    allow_chunked: bool = False,
) -> BodyPlan:
    """Validate framing fields and return the normalized body plan.

    RFC 9112 forbids forwarding/accepting Transfer-Encoding together with
    Content-Length because different parsers can disagree about the boundary.
    Repeated Content-Length is accepted only when every value is identical.
    """
    lengths = [item.strip() for value in content_lengths for item in value.split(",")]
    if any(not item for item in lengths):
        raise FramingError("Invalid Content-Length")

    encodings = [item.strip().lower() for value in transfer_encodings for item in value.split(",")]
    if any(not item for item in encodings):
        raise FramingError("Invalid Transfer-Encoding")
    if encodings and lengths:
        raise FramingError("Transfer-Encoding and Content-Length cannot be combined")
    if encodings:
        if encodings != ["chunked"]:
            raise FramingError("Unsupported Transfer-Encoding", HTTPStatus.NOT_IMPLEMENTED)
        if not allow_chunked:
            raise FramingError(
                "Chunked request bodies are not supported", HTTPStatus.NOT_IMPLEMENTED
            )
        return BodyPlan(None, chunked=True)

    if not lengths:
        return BodyPlan(0)
    if len(set(lengths)) != 1:
        raise FramingError("Conflicting Content-Length fields")
    text = lengths[0]
    if not text.isascii() or not text.isdigit():
        raise FramingError("Invalid Content-Length")
    length = int(text)
    if max_size is not None and length > max_size:
        raise FramingError("Request body exceeds the size limit", HTTPStatus.CONTENT_TOO_LARGE)
    return BodyPlan(length)


class LimitedReader:
    """A file-like view that cannot read beyond a validated body length."""

    def __init__(self, source: Readable, length: int) -> None:
        self._source = source
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        want = self.remaining if size < 0 else min(size, self.remaining)
        data = self._source.read(want)
        self.remaining -= len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        limit = self.remaining if size < 0 else min(size, self.remaining)
        readline = getattr(self._source, "readline", None)
        data = readline(limit) if readline is not None else self._source.read(limit)
        self.remaining -= len(data)
        return data

    def readlines(self, hint: int = -1) -> list[bytes]:
        return list(iter(self.readline, b""))

    def __iter__(self):
        return iter(self.readline, b"")

    def drain(self, limit: int | None = None) -> bool:
        """Consume the remainder up to ``limit``; return True when fully drained."""
        if limit is not None and self.remaining > limit:
            return False
        while self.remaining:
            if not self.read(min(64 * 1024, self.remaining)):
                break
        return self.remaining == 0

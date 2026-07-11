"""Shared HTTP request-body framing and bounded-reader primitives.

HTTP/1 adapters must agree on the declared body length and on whether a
connection is reusable.  This module contains the non-configurable framing rules;
callers supply their configurable size limit and decide how to report the error.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol


class Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class BodyStream(Readable, Protocol):
    def readline(self, size: int = -1, /) -> bytes: ...


class TimeoutSocket(Protocol):
    def gettimeout(self) -> float | None: ...
    def settimeout(self, value: float | None) -> None: ...


class FramingError(ValueError):
    """A request body is ambiguously or invalidly framed."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class BodyTimeoutError(TimeoutError):
    """The configured total request-body consumption budget expired."""


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


class DeadlineReader:
    """Apply one total deadline across blocking reads from a request body.

    The clock starts lazily on the first read. Each operation retains any
    shorter socket progress timeout and restores it before returning.
    """

    __slots__ = ("_buffer", "_deadline", "_sock", "_source", "_timeout")

    def __init__(
        self,
        source: BodyStream,
        sock: TimeoutSocket,
        timeout: float,
    ) -> None:
        self._source = source
        self._sock = sock
        self._timeout = timeout
        self._deadline: float | None = None
        self._buffer = b""

    def _remaining(self) -> float:
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + self._timeout
        remaining = self._deadline - now
        if remaining <= 0:
            raise BodyTimeoutError("request body deadline expired")
        return remaining

    def _read_once(self, size: int) -> bytes:
        remaining = self._remaining()
        previous = self._sock.gettimeout()
        total_is_shorter = previous is None or remaining <= previous
        effective = remaining if total_is_shorter else previous
        method: Callable[[int], bytes] = getattr(self._source, "read1", self._source.read)
        if previous != effective:
            self._sock.settimeout(effective)
        try:
            return method(size)
        except TimeoutError as exc:
            if total_is_shorter:
                raise BodyTimeoutError("request body deadline expired") from exc
            raise
        finally:
            if previous != effective:
                with contextlib.suppress(OSError):
                    self._sock.settimeout(previous)

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        self._remaining()
        chunks: list[bytes] = []
        if self._buffer:
            if size >= 0 and len(self._buffer) > size:
                data, self._buffer = self._buffer[:size], self._buffer[size:]
                return data
            chunks.append(self._buffer)
            self._buffer = b""
        if size < 0:
            while data := self._read_once(64 * 1024):
                chunks.append(data)
            return b"".join(chunks)
        remaining = size - sum(map(len, chunks))
        while remaining > 0:
            data = self._read_once(remaining)
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def readline(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        self._remaining()
        chunks: list[bytes] = []
        remaining = size
        while remaining != 0:
            newline = self._buffer.find(b"\n")
            take = len(self._buffer) if newline < 0 else newline + 1
            if remaining > 0:
                take = min(take, remaining)
            if take:
                chunks.append(self._buffer[:take])
                self._buffer = self._buffer[take:]
                if chunks[-1].endswith(b"\n") or (remaining > 0 and take == remaining):
                    break
                if remaining > 0:
                    remaining -= take
            want = 64 * 1024 if remaining < 0 else min(64 * 1024, remaining)
            data = self._read_once(want)
            if not data:
                break
            self._buffer += data
        return b"".join(chunks)

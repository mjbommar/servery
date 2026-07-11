"""Shared HTTP/1 request-header types and bounded incremental parsing."""

from __future__ import annotations

import contextlib
import io
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, overload

from servery import _body

MAX_REQUEST_LINE = 65536  # matches BaseHTTPRequestHandler's read limit
MAX_HEADER_LINE = 65536  # matches http.client._MAXLINE
MAX_HEADER_COUNT = 100  # matches http.client._MAXHEADERS
_FIELD_NAME_BYTES = rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_FIELD_VALUE_BYTES = rb"[\x09\x20-\x7e\x80-\xff]*"
_FIELD_BYTES = _FIELD_NAME_BYTES + rb":" + _FIELD_VALUE_BYTES
FIELD_BYTES_MATCH = re.compile(_FIELD_BYTES + rb"\Z").fullmatch
FIELD_BLOCK_MATCH = re.compile(
    rb"(?:[!#$%&'*+\-.^_`|~0-9A-Za-z]++:[\x09\x20-\x7e\x80-\xff]*+\r\n)*+\r\n\Z"
).fullmatch
_FIELD_LINE_RE = re.compile(_FIELD_BYTES + rb"\r?\n?\Z")
_CONTINUATION_LINE_RE = re.compile(rb"[ \t][\x09\x20-\x7e\x80-\xff]*\r?\n?\Z")
_HOST_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f@/?#,\\]")


class HeaderError(Exception):
    """A malformed or over-budget HTTP field block."""

    def __init__(
        self,
        message: str,
        status: HTTPStatus = HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
    ) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class RequestLine:
    """Parsed request-line facts needed by either connection adapter."""

    requestline: str
    method: str
    target: str
    version: str
    close_connection: bool
    has_headers: bool


class RequestLineError(ValueError):
    """A request-line error plus state the threaded adapter must preserve."""

    def __init__(
        self,
        status: HTTPStatus,
        message: str,
        *,
        response_version: str | None = None,
        close_connection: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.response_version = response_version
        self.close_connection = close_connection


class RequestHeadError(ValueError):
    """A semantic request-head error that must close the HTTP/1 connection."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class RequestHeadTimeoutError(TimeoutError):
    """The configured total request-head budget expired."""


class BufferedHeadStream(Protocol):
    """The narrow buffered-reader surface needed by ``HeadDeadlineReader``."""

    def peek(self, size: int = 0, /) -> bytes: ...
    def read(self, size: int = -1, /) -> bytes: ...


class HeadDeadlineReader:
    """Read request-head lines under one absolute deadline without over-reading.

    ``BufferedReader.readline()`` can perform several successful raw reads inside
    one call, so a socket progress timeout alone does not bound a slow-but-moving
    field line.  Peeking exposes each buffered/raw progress boundary; consuming
    only through the next newline leaves request bodies and pipelined heads in the
    original buffered reader.
    """

    __slots__ = ("_buffered", "_deadline", "_sock", "_source")

    def __init__(
        self,
        source: BufferedHeadStream,
        sock: _body.TimeoutSocket,
        timeout: float,
        buffered: bytes = b"",
    ) -> None:
        self._source = source
        self._sock = sock
        self._deadline = time.monotonic() + timeout
        # This is a non-owning snapshot from BufferedReader.peek(): bytes remain
        # in ``source`` until read() consumes exactly the selected line extent.
        self._buffered = buffered

    def _remaining(self) -> float:
        now = time.monotonic()
        remaining = self._deadline - now
        if remaining <= 0:
            raise RequestHeadTimeoutError("request head deadline expired")
        return remaining

    def _peek(self) -> bytes:
        remaining = self._remaining()
        previous = self._sock.gettimeout()
        total_is_shorter = previous is None or remaining <= previous
        effective = remaining if total_is_shorter else previous
        if previous != effective:
            self._sock.settimeout(effective)
        try:
            return self._source.peek(1)
        except TimeoutError as exc:
            if total_is_shorter:
                raise RequestHeadTimeoutError("request head deadline expired") from exc
            raise
        finally:
            if previous != effective:
                with contextlib.suppress(OSError):
                    self._sock.settimeout(previous)

    def buffered_head(self) -> bytes | None:
        """Consume and return a complete already-buffered head, if present."""
        lf_end = self._buffered.find(b"\n\n")
        crlf_end = self._buffered.find(b"\n\r\n")
        ends: list[int] = []
        if lf_end >= 0:
            ends.append(lf_end + 2)
        if crlf_end >= 0:
            ends.append(crlf_end + 3)
        if not ends:
            return None
        end = min(ends)
        data = self._source.read(end)
        self._buffered = self._buffered[len(data) :]
        return data

    def readline(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        chunks: list[bytes] = []
        remaining = size
        while remaining != 0:
            available = self._buffered or self._peek()
            if not available:
                break
            newline = available.find(b"\n")
            take = len(available) if newline < 0 else newline + 1
            if remaining > 0:
                take = min(take, remaining)
            data = self._source.read(take)
            if not data:
                break
            chunks.append(data)
            self._buffered = available[len(data) :]
            if data.endswith(b"\n"):
                break
            if remaining > 0:
                remaining -= len(data)
        return b"".join(chunks)


@dataclass(slots=True)
class RequestHead:
    """Transport-neutral policy result for one complete HTTP/1 request head."""

    request: RequestLine
    headers: RequestHeaders
    body: _body.BodyPlan
    close_connection: bool
    expect_continue: bool


def _version_policy(version: str, protocol_version: str) -> bool:
    """Return connection-close policy for a valid HTTP/1 version, or raise."""
    if version == "HTTP/1.1":
        return protocol_version < "HTTP/1.1"
    if version == "HTTP/1.0":
        return True
    if not version.startswith("HTTP/"):
        raise RequestLineError(HTTPStatus.BAD_REQUEST, f"Bad request version ({version!r})")
    base = version.split("/", 1)[1]
    parts = base.split(".")
    if (
        len(parts) != 2
        or any(not part.isdigit() for part in parts)
        or any(len(part) > 10 for part in parts)
    ):
        raise RequestLineError(HTTPStatus.BAD_REQUEST, f"Bad request version ({version!r})")
    number = (int(parts[0]), int(parts[1]))
    close = not (number >= (1, 1) and protocol_version >= "HTTP/1.1")
    if number >= (2, 0):
        raise RequestLineError(
            HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
            f"Invalid HTTP version ({base})",
            close_connection=close,
        )
    return close


def parse_request_line(
    raw_requestline: bytes,
    *,
    protocol_version: str = "HTTP/1.1",
    default_request_version: str = "HTTP/0.9",
) -> RequestLine | None:
    """Parse one request line with the stdlib-compatible servery policy."""
    requestline = raw_requestline.decode("iso-8859-1").rstrip("\r\n")
    words = requestline.split()
    if not words:
        return None

    version = default_request_version
    close_connection = True
    response_version: str | None = None
    if len(words) >= 3:
        version = words[-1]
        close_connection = _version_policy(version, protocol_version)
        response_version = version

    if not 2 <= len(words) <= 3:
        raise RequestLineError(
            HTTPStatus.BAD_REQUEST,
            f"Bad request syntax ({requestline!r})",
            response_version=response_version,
            close_connection=close_connection,
        )
    method, target = words[:2]
    if len(words) == 2:
        if method != "GET":
            raise RequestLineError(
                HTTPStatus.BAD_REQUEST, f"Bad HTTP/0.9 request type ({method!r})"
            )
        return RequestLine(requestline, method, target, version, True, False)

    # gh-87389: collapse a leading // so it cannot be interpreted as authority.
    if target.startswith("//"):
        target = "/" + target.lstrip("/")
    return RequestLine(
        requestline,
        method,
        target,
        version,
        close_connection,
        True,
    )


class RequestHeaders:
    """Minimal case-insensitive request-header map (first occurrence wins)."""

    __slots__ = ("_map", "_pairs")

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs
        mapping: dict[str, str] = {}
        for name, value in pairs:
            key = name.lower()
            if key not in mapping:
                mapping[key] = value
        self._map = mapping

    @overload
    def get(self, name: str) -> str | None: ...
    @overload
    def get(self, name: str, default: str) -> str: ...
    def get(self, name: str, default: str | None = None) -> str | None:
        return self._map.get(name.lower(), default)

    def __getitem__(self, name: str) -> str | None:
        return self._map.get(name.lower())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._map

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)

    def get_all(self, name: str) -> list[str]:
        key = name.lower()
        return [value for field, value in self._pairs if field.lower() == key]


def finalize_request_head(
    request: RequestLine,
    headers: RequestHeaders,
    *,
    protocol_version: str = "HTTP/1.1",
    max_body_size: int | None = None,
    allow_chunked: bool = False,
) -> RequestHead:
    """Apply shared framing, persistence, and Expect policy to parsed fields."""
    _validate_host(request, headers.get_all("Host"))
    body = _body.parse_framing(
        headers.get_all("Content-Length"),
        headers.get_all("Transfer-Encoding"),
        max_size=max_body_size,
        allow_chunked=allow_chunked,
    )
    close_connection, expect_continue = _connection_policy(
        request,
        headers.get("Connection", ""),
        headers.get("Expect", ""),
        protocol_version,
    )
    return RequestHead(request, headers, body, close_connection, expect_continue)


def _validate_host(request: RequestLine, hosts: list[str]) -> None:
    """Enforce the HTTP/1.1 Host cardinality and basic authority grammar."""
    if request.version not in {"HTTP/0.9", "HTTP/1.0"}:
        if len(hosts) != 1:
            raise RequestHeadError("HTTP/1.1 requires exactly one Host header")
        host = hosts[0]
        if (
            not host
            or not host.isascii()
            or _HOST_FORBIDDEN_RE.search(host) is not None
            or (host.startswith("[") and "]" not in host)
            or (not host.startswith("[") and host.count(":") > 1)
        ):
            raise RequestHeadError("Invalid Host header")
        if ":" in host and not host.startswith("["):
            hostname, port = host.rsplit(":", 1)
            if not hostname or not port.isascii() or not port.isdigit():
                raise RequestHeadError("Invalid Host header")
        elif host.startswith("["):
            closing = host.find("]")
            suffix = host[closing + 1 :]
            if not host[1:closing] or (suffix and not (suffix[1:].isdigit() and suffix[:1] == ":")):
                raise RequestHeadError("Invalid Host header")


def _connection_policy(
    request: RequestLine,
    connection: str,
    expect: str,
    protocol_version: str,
) -> tuple[bool, bool]:
    """Return close and 100-continue policy from normalized request fields."""
    close_connection = request.close_connection
    if connection.lower() == "close":
        close_connection = True
    elif connection.lower() == "keep-alive" and protocol_version >= "HTTP/1.1":
        close_connection = False
    expect_continue = (
        expect.lower() == "100-continue"
        and protocol_version >= "HTTP/1.1"
        and request.version >= "HTTP/1.1"
    )
    return close_connection, expect_continue


def _append_line(pairs: list[tuple[str, str]], line: bytes) -> None:
    if line[:1] in (b" ", b"\t"):
        if _CONTINUATION_LINE_RE.fullmatch(line) is None:
            raise HeaderError("Invalid folded header value", HTTPStatus.BAD_REQUEST)
        if pairs:
            name, value = pairs[-1]
            pairs[-1] = (name, f"{value} {line.strip().decode('latin-1')}")
        return
    if len(pairs) >= MAX_HEADER_COUNT:
        raise HeaderError("Too many headers")
    if _FIELD_LINE_RE.fullmatch(line) is None:
        raise HeaderError("Malformed header field", HTTPStatus.BAD_REQUEST)
    name, _, value = line.partition(b":")
    if value.endswith(b"\r\n"):
        value = value[:-2]
    elif value.endswith(b"\n"):
        value = value[:-1]
    value = value.strip(b" \t")
    pairs.append((name.decode("ascii"), value.decode("latin-1")))


def read_headers(rfile: io.BufferedIOBase) -> RequestHeaders:
    """Fast blocking adapter for buffered threaded handlers.

    Keep this loop specialized instead of routing each line through the
    incremental parser's helper.  Request headers are on every HTTP/1 hot path,
    and the extra Python call per field is measurable for tiny responses.
    """
    pairs: list[tuple[str, str]] = []
    while True:
        line = rfile.readline(MAX_HEADER_LINE + 1)
        if len(line) > MAX_HEADER_LINE:
            raise HeaderError("Header line too long")
        if line in (b"\r\n", b"\n", b""):
            break
        if line[:1] in (b" ", b"\t"):
            if _CONTINUATION_LINE_RE.fullmatch(line) is None:
                raise HeaderError("Invalid folded header value", HTTPStatus.BAD_REQUEST)
            if pairs:
                name, value = pairs[-1]
                pairs[-1] = (name, f"{value} {line.strip().decode('latin-1')}")
            continue
        if len(pairs) >= MAX_HEADER_COUNT:
            raise HeaderError("Too many headers")
        if _FIELD_LINE_RE.fullmatch(line) is None:
            raise HeaderError("Malformed header field", HTTPStatus.BAD_REQUEST)
        name, _, value = line.partition(b":")
        if value.endswith(b"\r\n"):
            value = value[:-2]
        elif value.endswith(b"\n"):
            value = value[:-1]
        value = value.strip(b" \t")
        pairs.append((name.decode("ascii"), value.decode("latin-1")))
    return RequestHeaders(pairs)


class HeaderBlockParser:
    """Incrementally parse one header block while preserving unconsumed bytes."""

    __slots__ = ("_buffer", "_complete", "_pairs")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pairs: list[tuple[str, str]] = []
        self._complete = False

    def feed(self, data: bytes) -> tuple[RequestHeaders | None, bytes]:
        """Consume ``data``; return headers and post-block bytes when complete."""
        if self._complete:
            raise RuntimeError("header parser is already complete")
        self._buffer.extend(data)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > MAX_HEADER_LINE:
                    raise HeaderError("Header line too long")
                return None, b""
            line_length = newline + 1
            if line_length > MAX_HEADER_LINE:
                raise HeaderError("Header line too long")
            line = bytes(self._buffer[:line_length])
            del self._buffer[:line_length]
            if line in (b"\r\n", b"\n"):
                self._complete = True
                remainder = bytes(self._buffer)
                self._buffer.clear()
                return RequestHeaders(self._pairs), remainder
            _append_line(self._pairs, line)

    def finish(self) -> RequestHeaders:
        """Finish at EOF, accepting the final unterminated line like ``readline``."""
        if self._complete:
            raise RuntimeError("header parser is already complete")
        if len(self._buffer) > MAX_HEADER_LINE:
            raise HeaderError("Header line too long")
        if self._buffer:
            _append_line(self._pairs, bytes(self._buffer))
            self._buffer.clear()
        self._complete = True
        return RequestHeaders(self._pairs)


class RequestHeadParser:
    """Incrementally parse one HTTP/1 request head and preserve following bytes."""

    __slots__ = (
        "_allow_chunked",
        "_complete",
        "_headers",
        "_line_buffer",
        "_max_body_size",
        "_protocol_version",
        "_request",
    )

    def __init__(
        self,
        *,
        protocol_version: str = "HTTP/1.1",
        max_body_size: int | None = None,
        allow_chunked: bool = False,
    ) -> None:
        self._protocol_version = protocol_version
        self._max_body_size = max_body_size
        self._allow_chunked = allow_chunked
        self._line_buffer = bytearray()
        self._headers: HeaderBlockParser | None = None
        self._request: RequestLine | None = None
        self._complete = False

    @property
    def complete(self) -> bool:
        """Whether a request head or an empty terminating line was consumed."""
        return self._complete

    def _finish_head(self, headers: RequestHeaders) -> RequestHead:
        request = self._request
        if request is None:  # pragma: no cover - internal state invariant
            raise RuntimeError("request line is not complete")
        self._complete = True
        return finalize_request_head(
            request,
            headers,
            protocol_version=self._protocol_version,
            max_body_size=self._max_body_size,
            allow_chunked=self._allow_chunked,
        )

    def _accept_request_line(self, line: bytes) -> RequestHead | None:
        request = parse_request_line(line, protocol_version=self._protocol_version)
        if request is None:
            self._complete = True
            return None
        self._request = request
        if not request.has_headers:
            return self._finish_head(RequestHeaders([]))
        self._headers = HeaderBlockParser()
        return None

    def feed(self, data: bytes) -> tuple[RequestHead | None, bytes]:
        """Consume a fragment; return a complete head and unconsumed bytes."""
        if self._complete:
            raise RuntimeError("request parser is already complete")
        if self._headers is None:
            self._line_buffer.extend(data)
            newline = self._line_buffer.find(b"\n")
            if newline < 0:
                if len(self._line_buffer) > MAX_REQUEST_LINE:
                    raise RequestLineError(HTTPStatus.REQUEST_URI_TOO_LONG, "Request-URI Too Long")
                return None, b""
            line_length = newline + 1
            if line_length > MAX_REQUEST_LINE:
                raise RequestLineError(HTTPStatus.REQUEST_URI_TOO_LONG, "Request-URI Too Long")
            line = bytes(self._line_buffer[:line_length])
            remainder = bytes(self._line_buffer[line_length:])
            self._line_buffer.clear()
            head = self._accept_request_line(line)
            if self._complete:
                return head, remainder
            data = remainder

        if not data:
            return None, b""
        headers_parser = self._headers
        if headers_parser is None:  # pragma: no cover - internal state invariant
            raise RuntimeError("header parser is not initialized")
        headers, remainder = headers_parser.feed(data)
        if headers is None:
            return None, b""
        return self._finish_head(headers), remainder

    def finish(self) -> RequestHead | None:
        """Finish at EOF, matching buffered-reader acceptance of final lines."""
        if self._complete:
            raise RuntimeError("request parser is already complete")
        if self._headers is None:
            if not self._line_buffer:
                self._complete = True
                return None
            if len(self._line_buffer) > MAX_REQUEST_LINE:
                raise RequestLineError(HTTPStatus.REQUEST_URI_TOO_LONG, "Request-URI Too Long")
            line = bytes(self._line_buffer)
            self._line_buffer.clear()
            head = self._accept_request_line(line)
            if self._complete:
                return head
        headers_parser = self._headers
        if headers_parser is None:  # pragma: no cover - internal state invariant
            raise RuntimeError("header parser is not initialized")
        return self._finish_head(headers_parser.finish())

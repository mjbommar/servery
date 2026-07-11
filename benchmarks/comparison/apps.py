"""Semantically identical WSGI and ASGI applications for external comparisons."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

_BODY = b"d" * 1024
_BODY_PATHS = {"/body/65536"}
_STREAMS: dict[str, tuple[bytes, int]] = {
    "/stream/65536": (b"r" * (4 * 1024), 16),
    "/stream/1048576": (b"r" * (64 * 1024), 16),
    "/stream/67108864": (b"r" * (64 * 1024), 1024),
}


def expected_body(path: str) -> bytes:
    """Return the response body shared by every supported comparison endpoint."""
    if path in {"/bytes/1024", "/sleep/10"} | _BODY_PATHS:
        return _BODY
    if path in _STREAMS:
        chunk, count = _STREAMS[path]
        return b"".join(_stream_chunk(chunk, index, count) for index in range(count))
    return b"not found"


def _stream_chunk(chunk: bytes, index: int, count: int) -> bytes:
    """Return realistic allocated chunks for the long slow-reader workload."""
    if count <= 16:
        return chunk
    return index.to_bytes(8, "big") + chunk[8:]


def _wsgi_stream(chunk: bytes, count: int) -> Iterable[bytes]:
    for index in range(count):
        yield _stream_chunk(chunk, index, count)


def wsgi_app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
    """Fixed-body WSGI app with an optional 10 ms blocking-I/O stand-in."""
    path = str(environ.get("PATH_INFO", "/"))
    if path in _BODY_PATHS:
        remaining = int(environ.get("CONTENT_LENGTH") or 0)
        source = environ["wsgi.input"]
        while remaining:
            data = source.read(min(64 * 1024, remaining))
            if not data:
                break
            remaining -= len(data)
    if path == "/sleep/10":
        time.sleep(0.010)
    stream = _STREAMS.get(path)
    body = expected_body(path) if stream is None else b""
    length = len(body) if stream is None else len(stream[0]) * stream[1]
    success = path in {"/bytes/1024", "/sleep/10"} | _BODY_PATHS or path in _STREAMS
    status = "200 OK" if success else "404 Not Found"
    start_response(
        status,
        [("Content-Type", "application/octet-stream"), ("Content-Length", str(length))],
    )
    if stream is not None:
        return _wsgi_stream(*stream)
    return [body]


async def asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI equivalent of :func:`wsgi_app`, including a nonblocking 10 ms wait."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            kind = message["type"]
            if kind == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif kind == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    assert scope["type"] == "http"
    path = str(scope.get("path", "/"))
    while (await receive()).get("more_body", False):
        pass
    if path == "/sleep/10":
        await asyncio.sleep(0.010)
    stream = _STREAMS.get(path)
    body = expected_body(path) if stream is None else b""
    length = len(body) if stream is None else len(stream[0]) * stream[1]
    success = path in {"/bytes/1024", "/sleep/10"} | _BODY_PATHS or path in _STREAMS
    status = 200 if success else 404
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(length).encode("ascii")),
            ],
        }
    )
    if stream is not None:
        chunk, count = stream
        for index in range(count):
            await send(
                {
                    "type": "http.response.body",
                    "body": _stream_chunk(chunk, index, count),
                    "more_body": index < count - 1,
                }
            )
        return
    await send({"type": "http.response.body", "body": body})

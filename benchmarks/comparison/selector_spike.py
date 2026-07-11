"""Benchmark-only asyncio static frontend used to test the connection model.

This deliberately supports only the comparison suite's plaintext GET/HEAD file
workload. It reuses servery's containment and validator helpers, but it is not a
second production server: there are no ranges, conditionals, compression,
uploads, WebDAV, proxying, TLS, access logs, or overload policy. Its purpose is
to estimate how much thread-per-connection costs before committing to a full
selector backend and its much larger correctness/test surface.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import mimetypes
import os
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

from servery import _conditional, _http1, _request, security

_MAX_LINE = 16 * 1024
_SMALL_BODY = 16 * 1024


def _response_head(
    status: str,
    headers: Sequence[tuple[str, str]],
    *,
    close: bool,
) -> bytes:
    lines = [f"HTTP/1.1 {status}\r\n", "Server: servery-selector-spike\r\n"]
    lines.append(f"Date: {_http1.http_date()}\r\n")
    lines.extend(f"{name}: {value}\r\n" for name, value in headers)
    if close:
        lines.append("Connection: close\r\n")
    lines.append("\r\n")
    return "".join(lines).encode("latin-1")


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, bool] | None:
    parser = _request.RequestHeadParser()
    head: _request.RequestHead | None = None
    while not parser.complete:
        line = await reader.readline()
        if not line:
            head = parser.finish()
            break
        head, remainder = parser.feed(line)
        if remainder:
            raise ValueError("unexpected bytes after header block")
    if head is None:
        return None
    # The benchmark frontend has no request-body consumer. Close after the
    # response when a body was declared so following bytes cannot be re-parsed.
    has_body = head.body.chunked or bool(head.body.length)
    return head.request.method, head.request.target, head.close_connection or has_body


async def _serve_file(
    writer: asyncio.StreamWriter,
    root_real: str,
    method: str,
    target: str,
    *,
    close: bool,
) -> None:
    path = security.safe_join(root_real, urllib.parse.urlsplit(target).path)
    try:
        if path is None:
            raise FileNotFoundError
        source = open(path, "rb")  # noqa: PTH123, SIM115 - closed after async send
    except OSError:
        writer.write(
            _response_head(
                "404 Not Found",
                (("Content-Length", "0"), ("X-Content-Type-Options", "nosniff")),
                close=close,
            )
        )
        await writer.drain()
        return

    with source:
        stat = os.fstat(source.fileno())
        content_type = mimetypes.guess_file_type(path)[0] or "application/octet-stream"
        headers = (
            ("Content-Type", content_type),
            ("Content-Length", str(stat.st_size)),
            ("Accept-Ranges", "bytes"),
            ("ETag", _conditional.make_etag(stat)),
            ("Last-Modified", _http1.format_http_date(stat.st_mtime)),
            ("X-Content-Type-Options", "nosniff"),
        )
        head = _response_head("200 OK", headers, close=close)
        if method == "HEAD" or stat.st_size == 0:
            writer.write(head)
            await writer.drain()
        elif stat.st_size <= _SMALL_BODY:
            writer.write(head + source.read(stat.st_size))
            await writer.drain()
        else:
            writer.write(head)
            await writer.drain()
            loop = asyncio.get_running_loop()
            await loop.sendfile(writer.transport, source, 0, stat.st_size)


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    root_real: str,
) -> None:
    close = True
    try:
        while True:
            request = await _read_request(reader)
            if request is None:
                return
            method, target, close = request
            if method not in {"GET", "HEAD"}:
                writer.write(
                    _response_head(
                        "405 Method Not Allowed",
                        (("Allow", "GET, HEAD"), ("Content-Length", "0")),
                        close=True,
                    )
                )
                await writer.drain()
                return
            await _serve_file(writer, root_real, method, target, close=close)
            if close:
                return
    except (
        ConnectionError,
        OSError,
        ValueError,
        _request.HeaderError,
        asyncio.CancelledError,
    ):
        return
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


async def _run(directory: Path, host: str, port: int) -> None:
    root_real = os.path.realpath(directory)
    server = await asyncio.start_server(
        lambda reader, writer: _handle(reader, writer, root_real),
        host,
        port,
        backlog=128,
        limit=_MAX_LINE,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.directory, args.bind, args.port))


if __name__ == "__main__":
    main()

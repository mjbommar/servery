"""ASGI 3.0 hosting (experimental) — opt-in via ``--asgi module:app``.

ASGI is asynchronous, so unlike WSGI/CGI it cannot ride servery's synchronous
thread-per-connection handler. This module is a small, self-contained asyncio
HTTP/1.1 server (``asyncio.start_server`` + a minimal request parser) that maps
each request to an ASGI ``scope`` + ``receive``/``send`` and runs the lifespan
protocol — a "mini-uvicorn" in pure stdlib. Zero runtime dependencies; the hosted
app brings its own.

Scope: the HTTP ASGI scope with keep-alive, Content-Length or chunked framing,
and lifespan. WebSocket and TLS are not handled yet (HTTP, cleartext).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from http import HTTPStatus
from typing import Any

from servery import _log

_MAX_BODY = 100 * 1024 * 1024
_INTERNAL_ERROR = (
    b"HTTP/1.1 500 Internal Server Error\r\n"
    b"Content-Type: text/plain\r\nContent-Length: 21\r\nConnection: close\r\n\r\n"
    b"Internal Server Error"
)


def load_app(spec: str) -> Any:
    """Import an ASGI app from ``"module:attribute"`` (default attr ``app``)."""
    module_name, _, attr = spec.partition(":")
    if not module_name:
        raise ValueError(f"invalid --asgi spec {spec!r} (expected 'module:app')")
    module = importlib.import_module(module_name)
    app = getattr(module, attr or "app", None)
    if app is None:
        raise ValueError(f"--asgi: {module_name!r} has no attribute {attr or 'app'!r}")
    if not callable(app):
        raise ValueError(f"--asgi: {spec!r} is not callable")  # noqa: TRY004 (CLI value error)
    return app


class _Lifespan:
    """Drive the ASGI lifespan protocol; degrade gracefully if unsupported."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._startup = asyncio.Event()  # set on startup.complete OR app exit
        self._shutdown = asyncio.Event()
        self._startup_ok = False  # True only if the app sent startup.complete
        self._error: BaseException | None = None
        self._task: asyncio.Task[Any] | None = None

    async def _receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "lifespan.startup.complete":
            self._startup_ok = True
            self._startup.set()
        elif kind == "lifespan.startup.failed":
            self._startup.set()
        elif kind == "lifespan.shutdown.complete":
            self._shutdown.set()

    async def startup(self) -> None:
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}

        async def runner() -> None:
            try:
                await self._app(scope, self._receive, self._send)
            except Exception as exc:  # app may not support lifespan at all
                self._error = exc
            self._startup.set()  # unblock startup() if the app exited early
            self._shutdown.set()

        self._task = asyncio.ensure_future(runner())
        await self._inbox.put({"type": "lifespan.startup"})
        waiter = asyncio.ensure_future(self._startup.wait())
        await asyncio.wait({self._task, waiter}, return_when=asyncio.FIRST_COMPLETED, timeout=5.0)
        waiter.cancel()
        self._startup.set()  # proceed regardless — lifespan is best-effort
        if not self._startup_ok:
            _log.logger.debug(
                "ASGI lifespan not completed (unsupported or failed): %r", self._error
            )

    async def shutdown(self) -> None:
        if self._startup_ok and self._task is not None and not self._task.done():
            await self._inbox.put({"type": "lifespan.shutdown"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._shutdown.wait(), timeout=5.0)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    """Decode a client chunked request body (RFC 9112 §7.1), bounded by _MAX_BODY."""
    chunks: list[bytes] = []
    total = 0
    while True:
        size_line = await reader.readuntil(b"\r\n")
        size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)  # ignore extensions
        if size == 0:
            while (await reader.readuntil(b"\r\n")) != b"\r\n":  # consume any trailers
                pass
            break
        total += size
        if total > _MAX_BODY:
            raise ValueError("chunked request body exceeds limit")
        chunks.append(await reader.readexactly(size))
        await reader.readexactly(2)  # the CRLF that terminates each chunk
    return b"".join(chunks)


def _wants_keep_alive(version: str, headers: dict[bytes, bytes]) -> bool:
    conn = headers.get(b"connection", b"").lower()
    if version >= "HTTP/1.1":
        return conn != b"close"
    return conn == b"keep-alive"


class _Exchange:
    """One ASGI request/response over an asyncio stream pair."""

    def __init__(self, app: Any, server_addr: tuple[str, int]) -> None:
        self._app = app
        self._server_addr = server_addr

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while await self._handle_one(reader, writer):
                pass
        except (
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        try:
            request_line = await reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return False
        fields = request_line.decode("latin-1").split()
        if len(fields) != 3:
            return False  # empty keep-alive probe or malformed line -> close
        method, raw_path, version = fields
        headers: list[tuple[bytes, bytes]] = []
        header_map: dict[bytes, bytes] = {}
        while True:
            line = await reader.readuntil(b"\r\n")
            if line == b"\r\n":
                break
            name, _, value = line.partition(b":")
            key, val = name.strip().lower(), value.strip()
            headers.append((key, val))
            header_map[key] = val
        keep_alive = _wants_keep_alive(version, header_map)
        chunked = b"chunked" in header_map.get(b"transfer-encoding", b"").lower()
        try:
            if chunked:
                body = await _read_chunked(reader)
            else:
                length = min(int(header_map.get(b"content-length", b"0") or 0), _MAX_BODY)
                body = await reader.readexactly(length) if length else b""
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return False  # malformed length/framing -> close
        path, _, query = raw_path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": version.split("/", 1)[1] if "/" in version else "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": raw_path.encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "headers": headers,
            "server": list(self._server_addr),
            "client": list(writer.get_extra_info("peername", ("", 0))[:2]),
        }
        state = _ResponseState(writer, method, keep_alive)
        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        try:
            await self._app(scope, receive, state.send)
        except Exception:
            # The app raised out of its coroutine (it didn't handle its own error).
            # Log with traceback; send a 500 if we haven't committed a response yet.
            _log.logger.error(
                'ASGI app error: %s "%s %s"', scope["client"][0], method, raw_path, exc_info=True
            )
            if not state.started:
                with contextlib.suppress(OSError):
                    writer.write(_INTERNAL_ERROR)
            with contextlib.suppress(OSError):
                await writer.drain()
            return False
        await writer.drain()
        if _log.logger.isEnabledFor(logging.INFO):
            _log.logger.info(
                '%s "%s %s %s" %s', scope["client"][0], method, raw_path, version, state.status
            )
        return keep_alive and not state.close


class _ResponseState:
    __slots__ = ("_writer", "chunked", "close", "headers", "method", "started", "status")

    def __init__(self, writer: asyncio.StreamWriter, method: str, keep_alive: bool) -> None:
        self._writer = writer
        self.method = method
        self.close = not keep_alive
        self.status = 200
        self.headers: list[tuple[bytes, bytes]] = []
        self.started = False
        self.chunked = False

    async def send(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "http.response.start":
            self.status = event["status"]
            self.headers = list(event.get("headers", []))
        elif kind == "http.response.body":
            if not self.started:
                self._write_headers()
                self.started = True
            data = event.get("body", b"")
            if data and self.method != "HEAD":
                self._writer.write(b"%x\r\n%s\r\n" % (len(data), data) if self.chunked else data)
            if not event.get("more_body", False) and self.chunked:
                self._writer.write(b"0\r\n\r\n")

    def _write_headers(self) -> None:
        present = {name.lower() for name, _ in self.headers}
        try:
            reason = HTTPStatus(self.status).phrase
        except ValueError:
            reason = ""
        lines = [f"HTTP/1.1 {self.status} {reason}".encode("latin-1")]
        lines += [name + b": " + value for name, value in self.headers]
        if b"content-length" in present:
            pass
        elif not self.close and self.method != "HEAD":
            self.chunked = True
            lines.append(b"Transfer-Encoding: chunked")
        else:
            self.close = True
        if self.close:
            lines.append(b"Connection: close")
        self._writer.write(b"\r\n".join(lines) + b"\r\n\r\n")


async def serve_forever(
    config: Any,
    *,
    started: Any = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Run the ASGI server. ``started(sockname)`` fires once bound; ``stop`` ends it."""
    app = load_app(config.asgi_app)
    lifespan = _Lifespan(app)
    await lifespan.startup()
    server = await asyncio.start_server(
        lambda r, w: _Exchange(app, server.sockets[0].getsockname()[:2]).handle_connection(r, w),
        config.host,
        config.port,
    )
    try:
        if started is not None:
            started(server.sockets[0].getsockname())
        async with server:
            if stop is not None:
                await stop.wait()
            else:  # pragma: no cover - CLI runs until interrupted
                await server.serve_forever()
    finally:
        await lifespan.shutdown()


def run(config: Any) -> None:  # pragma: no cover - blocking CLI entry
    """Run the ASGI server until interrupted (CLI entry)."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_forever(config))

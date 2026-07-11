"""ASGI 3.0 hosting (experimental) — opt-in via ``--asgi module:app``.

ASGI is asynchronous, so unlike WSGI/CGI it cannot ride servery's synchronous
thread-per-connection handler. This module is a small, self-contained asyncio
HTTP/1.1 server (``asyncio.start_server`` + a minimal request parser) that maps
each request to an ASGI ``scope`` + ``receive``/``send`` and runs the lifespan
protocol — a "mini-uvicorn" in pure stdlib. Zero runtime dependencies; the hosted
app brings its own.

Scopes: the HTTP ASGI scope (keep-alive, Content-Length or chunked framing) and
the WebSocket scope (RFC 6455, via ``servery._websocket``), plus lifespan and TLS
(shared with the threading server via ``_tls``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, cast

from servery import (
    __version__,
    _appspec,
    _body,
    _http1,
    _listener,
    _log,
    _request,
    _tls,
    _websocket,
    _write,
    auth,
)

_SERVER_HEADER = f"Server: servery/{__version__}".encode("latin-1")
_HOST_FORBIDDEN = bytes(range(0x21)) + b"\x7f@/?#,\\"
_PER_FIELD_VALIDATION_LIMIT = 8
_FIELD_BYTES_MATCH = _request.FIELD_BYTES_MATCH
_FIELD_BLOCK_MATCH = _request.FIELD_BLOCK_MATCH
_FIELD_VALUE_FORBIDDEN = bytes(range(9)) + bytes(range(10, 32)) + b"\x7f"
_COMMON_RESPONSE_PRESENT = frozenset((b"content-type", b"content-length"))
_COMMON_CONTENT_TYPE = b"application/octet-stream"
_TRAILER_FORBIDDEN = frozenset(
    (
        b"connection",
        b"content-length",
        b"host",
        b"keep-alive",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    )
)


def _valid_host(host: bytes) -> bool:
    """Return whether one byte-native Host value has valid authority shape."""
    if not host or not host.isascii() or len(host.translate(None, _HOST_FORBIDDEN)) != len(host):
        return False
    if host.startswith(b"["):
        closing = host.find(b"]")
        if closing < 0 or not host[1:closing]:
            return False
        suffix = host[closing + 1 :]
        return not suffix or (suffix.startswith(b":") and suffix[1:].isdigit())
    if host.count(b":") > 1:
        return False
    if b":" in host:
        hostname, port = host.rsplit(b":", 1)
        return bool(hostname and port.isdigit())
    return True


def _response_trailers(values: Any) -> list[tuple[bytes, bytes]]:
    """Validate and materialize one ASGI response trailer iterable."""
    headers: list[tuple[bytes, bytes]] = []
    for item in values:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError("ASGI response headers must contain name/value pairs")
        name, value = item
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise TypeError("ASGI response header names and values must be bytes")
        if name != name.lower() or _FIELD_BYTES_MATCH(name + b":" + value) is None:
            raise ValueError("Invalid ASGI response trailer field")
        if name in _TRAILER_FORBIDDEN:
            raise ValueError("ASGI response trailer cannot alter routing or framing")
        headers.append((name, value))
    return headers


def load_app(spec: str) -> Any:
    """Import an ASGI app from ``"module:attribute"`` (attr defaults to ``app``)."""
    return _appspec.load_app(spec, default_attr="app", label="--asgi")


class LifespanError(ValueError):
    """An explicit, malformed, or timed-out ASGI lifespan failure."""


class _Lifespan:
    """Drive ASGI lifespan with explicit auto/on policy and bounded waits."""

    def __init__(self, app: Any, *, mode: str = "auto", timeout: float = 5.0) -> None:
        self._app = app
        self._mode = mode
        self._timeout = timeout
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._startup = asyncio.Event()  # set on startup.complete OR app exit
        self._shutdown = asyncio.Event()
        self._startup_ok = False  # True only if the app sent startup.complete
        self._startup_failed: str | None = None
        self._shutdown_failed: str | None = None
        self._error: BaseException | None = None
        self._task: asyncio.Task[Any] | None = None
        self.state: dict[str, Any] = {}

    async def _receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "lifespan.startup.complete":
            if self._startup.is_set():
                raise ValueError("duplicate ASGI lifespan startup result")
            self._startup_ok = True
            self._startup.set()
        elif kind == "lifespan.startup.failed":
            if self._startup.is_set():
                raise ValueError("duplicate ASGI lifespan startup result")
            self._startup_failed = str(message.get("message", ""))
            self._startup.set()
        elif kind == "lifespan.shutdown.complete":
            if not self._startup_ok or self._shutdown.is_set():
                raise ValueError("ASGI lifespan shutdown result is out of order")
            self._shutdown.set()
        elif kind == "lifespan.shutdown.failed":
            if not self._startup_ok or self._shutdown.is_set():
                raise ValueError("ASGI lifespan shutdown result is out of order")
            self._shutdown_failed = str(message.get("message", ""))
            self._shutdown.set()
        else:
            raise ValueError(f"unsupported ASGI lifespan message: {kind!r}")

    async def _cancel_task(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def startup(self) -> bool:
        """Return whether lifespan is supported; raise on required/explicit failure."""
        scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": self.state,
        }

        async def runner() -> None:
            try:
                await self._app(scope, self._receive, self._send)
            except BaseException as exc:  # unsupported apps may reject this scope
                self._error = exc
            self._startup.set()  # unblock startup() if the app exited early
            self._shutdown.set()

        self._task = asyncio.ensure_future(runner())
        await self._inbox.put({"type": "lifespan.startup"})
        try:
            await asyncio.wait_for(self._startup.wait(), timeout=self._timeout)
        except TimeoutError:
            await self._cancel_task()
            raise LifespanError(
                f"ASGI lifespan startup timed out after {self._timeout:g} seconds"
            ) from None
        if self._startup_failed is not None:
            message = f": {self._startup_failed}" if self._startup_failed else ""
            await self._cancel_task()
            raise LifespanError(f"ASGI lifespan startup failed{message}")
        if self._startup_ok:
            return True
        if self._mode == "auto":
            _log.logger.debug("ASGI lifespan appears unsupported: %r", self._error)
            await self._cancel_task()
            return False
        error = self._error
        await self._cancel_task()
        detail = f": {error}" if error is not None else ""
        raise LifespanError(f"ASGI lifespan startup exited without completing{detail}") from error

    async def shutdown(self) -> None:
        if not self._startup_ok or self._task is None:
            await self._cancel_task()
            return
        if self._task.done():
            error = self._error
            detail = f": {error}" if error is not None else ""
            await self._cancel_task()
            raise LifespanError(
                f"ASGI lifespan application exited before shutdown{detail}"
            ) from error
        try:
            await self._inbox.put({"type": "lifespan.shutdown"})
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self._timeout)
            except TimeoutError:
                raise LifespanError(
                    f"ASGI lifespan shutdown timed out after {self._timeout:g} seconds"
                ) from None
            if self._shutdown_failed is not None:
                message = f": {self._shutdown_failed}" if self._shutdown_failed else ""
                raise LifespanError(f"ASGI lifespan shutdown failed{message}")
            if self._error is not None:
                raise LifespanError(
                    f"ASGI lifespan shutdown exited without completing: {self._error}"
                ) from self._error
        finally:
            await self._cancel_task()


class _BodyReadError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class ClientDisconnectError(OSError):
    """An ASGI HTTP send targeted a completed scope or closed peer."""


class _Connection:
    """Lifecycle state for one accepted ASGI transport."""

    __slots__ = ("phase", "task", "writer")

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.task = cast("asyncio.Task[Any]", asyncio.current_task())
        # ``idle`` includes a connection waiting for its first or next request
        # head.  It may be closed immediately once draining begins.  ``http``
        # and ``websocket`` are admitted application work and receive grace.
        self.phase = "idle"


class _Drain:
    """Track accepted transports and coordinate bounded graceful shutdown."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self._connections: set[_Connection] = set()

    @property
    def draining(self) -> bool:
        return self.event.is_set()

    def register(self, writer: asyncio.StreamWriter) -> _Connection | None:
        if self.draining:
            writer.close()
            return None
        connection = _Connection(writer)
        self._connections.add(connection)
        return connection

    def unregister(self, connection: _Connection) -> None:
        self._connections.discard(connection)

    def begin(self) -> None:
        """Stop request admission and wake connections blocked between requests."""
        if self.draining:
            return
        self.event.set()
        for connection in tuple(self._connections):
            if connection.phase == "idle":
                connection.writer.close()

    async def finish(self, timeout: float) -> None:
        """Give admitted work grace, then cancel tasks and abort their transports."""
        self.begin()
        current = asyncio.current_task()
        tasks = {
            connection.task
            for connection in self._connections
            if connection.task is not current and not connection.task.done()
        }
        if tasks and timeout:
            _, tasks = await asyncio.wait(tasks, timeout=timeout)
        if not tasks:
            return
        phases: dict[str, int] = {}
        for connection in self._connections:
            if connection.task in tasks:
                phases[connection.phase] = phases.get(connection.phase, 0) + 1
        _log.logger.warning(
            "ASGI graceful drain deadline reached; force-cancelling %d "
            "connection task(s), phases=%s",
            len(tasks),
            ",".join(f"{name}:{phases[name]}" for name in sorted(phases)),
        )
        for connection in tuple(self._connections):
            if connection.task in tasks:
                connection.task.cancel()
                connection.writer.transport.abort()
        # Deliver cancellation once, but do not let an application that catches
        # and suppresses CancelledError extend the configured drain deadline.
        # A future multi-process supervisor provides the hard containment
        # boundary for cancellation-resistant Python application code.
        await asyncio.sleep(0)
        for task in tasks:
            if task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
            else:
                task.add_done_callback(_observe_task)
        resistant = sum(not task.done() for task in tasks)
        if resistant:
            _log.logger.warning(
                "%d ASGI connection task(s) suppressed forced cancellation; "
                "worker termination is required",
                resistant,
            )


def _observe_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a late connection-task result without extending shutdown."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _set_tcp_nodelay(writer: asyncio.StreamWriter) -> None:
    """Keep small ASGI response writes out of Nagle/delayed-ACK stalls."""
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is not None:
        with contextlib.suppress(OSError):
            transport_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _DisconnectState:
    """Lazy EOF observer installed only while an application is listening."""

    __slots__ = (
        "_disconnected",
        "_original_eof",
        "_original_lost",
        "_protocol",
        "_reader",
        "_waiters",
    )

    def __init__(
        self,
        protocol: asyncio.StreamReaderProtocol | None = None,
        reader: asyncio.StreamReader | None = None,
    ) -> None:
        self._disconnected = False
        self._protocol = protocol
        self._reader = reader
        self._waiters: set[asyncio.Future[None]] | None = None
        self._original_eof: Callable[[], bool | None] | None = None
        self._original_lost: Callable[[Exception | None], None] | None = None

    def subscribe(self) -> asyncio.Future[None]:
        waiter = asyncio.get_running_loop().create_future()
        if self._disconnected:
            waiter.set_result(None)
        else:
            if self._reader is not None and self._reader.at_eof():
                self._disconnected = True
                waiter.set_result(None)
                return waiter
            if self._waiters is None:
                self._waiters = set()
            self._waiters.add(waiter)
            if self._original_eof is None:
                self._install()
        return waiter

    def unsubscribe(self, waiter: asyncio.Future[None]) -> None:
        if self._waiters is not None:
            self._waiters.discard(waiter)
            if not self._waiters and not self._disconnected:
                self._restore()

    def disconnect(self) -> None:
        self._disconnected = True
        self.wake()

    def wake(self) -> None:
        if self._waiters is not None:
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_result(None)
            self._waiters.clear()

    def _install(self) -> None:
        protocol = self._protocol
        if protocol is None:
            return
        original_eof = protocol.eof_received
        original_lost = protocol.connection_lost
        self._original_eof = original_eof
        self._original_lost = original_lost

        def eof_received() -> bool | None:
            try:
                return original_eof()
            finally:
                self.disconnect()

        def connection_lost(exc: Exception | None) -> None:
            try:
                original_lost(exc)
            finally:
                self.disconnect()

        dynamic_protocol = cast("Any", protocol)
        dynamic_protocol.eof_received = eof_received
        dynamic_protocol.connection_lost = connection_lost

    def _restore(self) -> None:
        protocol = self._protocol
        if protocol is None or self._original_eof is None or self._original_lost is None:
            return
        dynamic_protocol = cast("Any", protocol)
        dynamic_protocol.eof_received = self._original_eof
        dynamic_protocol.connection_lost = self._original_lost
        self._original_eof = None
        self._original_lost = None


class _ResponseCompletion:
    """Lazy response-completion subscribers sharing the body-complete callable."""

    __slots__ = ("body_complete", "waiters")

    def __init__(self, body_complete: Callable[[], bool]) -> None:
        self.body_complete = body_complete
        self.waiters: set[asyncio.Future[None]] = set()

    def __call__(self) -> bool:
        return self.body_complete()

    def subscribe(self) -> asyncio.Future[None]:
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.add(waiter)
        return waiter

    def unsubscribe(self, waiter: asyncio.Future[None]) -> None:
        self.waiters.discard(waiter)

    def wake(self) -> None:
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)
        self.waiters.clear()


class _AsyncBody:
    """Expose an accepted HTTP/1 body as bounded ASGI ``receive()`` chunks."""

    _CHUNK = 64 * 1024
    _MAX_TRAILERS = 64 * 1024

    def __init__(
        self,
        reader: asyncio.StreamReader,
        plan: _body.BodyPlan,
        *,
        max_body: int,
        timeout: float,
        disconnected: _DisconnectState | asyncio.StreamReaderProtocol | None = None,
    ) -> None:
        self._reader = reader
        self._chunked = plan.chunked
        self._remaining = plan.length or 0
        self._chunk_remaining = 0
        self._total = 0
        self._max_body = max_body
        self._timeout = timeout
        self._disconnected = disconnected
        self._response: _ResponseState | None = None
        self._done = not plan.chunked and self._remaining == 0
        self._final_delivered = False

    @property
    def complete(self) -> bool:
        return self._done

    def __call__(self) -> bool:
        return self._done

    async def receive(self) -> dict[str, Any]:
        if self._final_delivered:
            return await self._receive_terminal()
        if self._done:
            self._final_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        try:
            if not self._chunked:
                size = min(self._CHUNK, self._remaining)
                timeout = self._read_timeout()
                data = await asyncio.wait_for(self._reader.readexactly(size), timeout)
                self._remaining -= len(data)
                self._done = self._remaining == 0
                if self._done:
                    self._final_delivered = True
                return {"type": "http.request", "body": data, "more_body": not self._done}
            return await self._receive_chunked()
        except (asyncio.IncompleteReadError, ConnectionError):
            self._done = True
            self._final_delivered = True
            if isinstance(self._disconnected, _DisconnectState):
                self._disconnected.disconnect()
            return {"type": "http.disconnect"}

    def _read_timeout(self) -> float:
        return self._timeout

    async def _receive_terminal(self) -> dict[str, Any]:
        response = self._response
        response_waiter = response.subscribe_completion() if response is not None else None
        if response_waiter is not None and response_waiter.done():
            return {"type": "http.disconnect"}
        disconnected = self._disconnected
        if isinstance(disconnected, asyncio.StreamReaderProtocol):
            state = getattr(disconnected, "_servery_disconnect_state", None)
            if not isinstance(state, _DisconnectState):
                state = _DisconnectState(disconnected, self._reader)
                cast("Any", disconnected)._servery_disconnect_state = state
            disconnected = state
        elif disconnected is None:
            disconnected = _DisconnectState(reader=self._reader)
            self._disconnected = disconnected
        waiter = disconnected.subscribe()
        try:
            if response_waiter is None:
                await waiter
            else:
                await asyncio.wait(
                    (waiter, response_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            disconnected.unsubscribe(waiter)
            if response is not None and response_waiter is not None:
                response.unsubscribe_completion(response_waiter)
        return {"type": "http.disconnect"}

    async def _receive_chunked(self) -> dict[str, Any]:
        if self._chunk_remaining == 0:
            timeout = self._read_timeout()
            raw = await asyncio.wait_for(self._reader.readuntil(b"\r\n"), timeout)
            token = raw[:-2].split(b";", 1)[0].strip()
            if not token or any(byte not in b"0123456789abcdefABCDEF" for byte in token):
                raise _BodyReadError("invalid chunk size")
            # The byte allowlist above makes conversion infallible and avoids
            # accepting Python-specific integer spellings.
            size = int(token, 16)
            if size == 0:
                await self._consume_trailers()
                self._done = True
                self._final_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            self._total += size
            if self._total > self._max_body:
                raise _BodyReadError("chunked request body exceeds limit", 413)
            self._chunk_remaining = size
        size = min(self._CHUNK, self._chunk_remaining)
        timeout = self._read_timeout()
        data = await asyncio.wait_for(self._reader.readexactly(size), timeout)
        self._chunk_remaining -= len(data)
        if self._chunk_remaining == 0:
            timeout = self._read_timeout()
            terminator = await asyncio.wait_for(self._reader.readexactly(2), timeout)
            if terminator != b"\r\n":
                raise _BodyReadError("invalid chunk terminator")
        # Preserve streaming: a final empty receive follows after the zero chunk.
        return {"type": "http.request", "body": data, "more_body": True}

    async def _consume_trailers(self) -> None:
        total = 0
        while True:
            timeout = self._read_timeout()
            line = await asyncio.wait_for(self._reader.readuntil(b"\r\n"), timeout)
            total += len(line)
            if total > self._MAX_TRAILERS:
                raise _BodyReadError("request trailers exceed limit", 431)
            if line == b"\r\n":
                return


class _TimedAsyncBody(_AsyncBody):
    """ASGI request body with one total budget layered over progress waits."""

    def __init__(self, *args: Any, body_timeout: float, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._body_timeout = body_timeout
        self._deadline: float | None = None

    def _read_timeout(self) -> float:
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + self._body_timeout
        remaining = self._deadline - now
        if remaining <= 0:
            raise _body.BodyTimeoutError("request body deadline expired")
        return min(self._timeout, remaining)


def _wants_keep_alive(version: str, headers: dict[bytes, bytes]) -> bool:
    conn = headers.get(b"connection", b"").lower()
    if version >= "HTTP/1.1":
        return conn != b"close"
    return conn == b"keep-alive"


class _Exchange:
    """One ASGI request/response over an asyncio stream pair."""

    def __init__(
        self,
        app: Any,
        server_addr: tuple[str, int],
        scheme: str = "http",
        credential: auth.Credential | None = None,
        timeout: float = 30.0,
        keepalive_timeout: float | None = None,
        request_head_timeout: float | None = None,
        request_body_timeout: float | None = None,
        write_timeout: float | None = None,
        max_body: int = 100 * 1024 * 1024,
        max_requests: int = 0,
        policy: list[tuple[bytes, bytes]] | None = None,
        lifespan_state: dict[str, Any] | None = None,
        drain: _Drain | None = None,
        connection: _Connection | None = None,
    ) -> None:
        self._app = app
        self._server_addr = server_addr
        self._scheme = scheme
        self._credential = credential
        self._timeout = timeout
        self._keepalive_timeout = keepalive_timeout
        self._request_head_timeout = request_head_timeout
        self._request_body_timeout = request_body_timeout
        self._write_timeout = write_timeout
        self._max_body = max_body
        self._max_requests = max_requests
        self._policy = policy or []
        self._lifespan_state = lifespan_state
        self._drain = drain
        self._connection = connection

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        disconnected: _DisconnectState | asyncio.StreamReaderProtocol | None = None,
    ) -> None:
        try:
            requests = 0
            if self._keepalive_timeout is None and self._request_head_timeout is None:
                while True:
                    if self._drain is not None and self._drain.draining:
                        break
                    if self._max_requests:
                        requests += 1
                    if not await self._handle_one(reader, writer, disconnected, requests):
                        break
                return

            first_request = True
            while True:
                if self._drain is not None and self._drain.draining:
                    break
                if self._max_requests:
                    requests += 1
                idle_timeout = (
                    self._timeout
                    if first_request or self._keepalive_timeout is None
                    else self._keepalive_timeout
                )
                total_timeout = self._request_head_timeout
                head = await self._read_phased_head(
                    reader,
                    idle_timeout,
                    self._timeout if total_timeout is None else min(self._timeout, total_timeout),
                )
                if not await self._handle_one(
                    reader,
                    writer,
                    disconnected,
                    requests,
                    head,
                ):
                    break
                first_request = False
        except TimeoutError:
            # A read or write progress deadline has expired. Abort instead of
            # asking writer.close() to gracefully flush bytes already queued to
            # the stalled peer; the deadline is meant to release this slot now.
            writer.transport.abort()
        except (
            *_tls.CLIENT_TRANSPORT_ERRORS,  # dropped conn / failed TLS handshake / timeout
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _read_phased_head(
        reader: asyncio.StreamReader,
        idle_timeout: float,
        head_timeout: float,
    ) -> bytes:
        """Wait for first activity, then reschedule the same timer for the head."""
        async with asyncio.timeout(idle_timeout) as phase:
            first = await reader.readexactly(1)
            phase.reschedule(asyncio.get_running_loop().time() + head_timeout)
            return first + await reader.readuntil(b"\r\n\r\n")

    async def _handle_one(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        disconnected: _DisconnectState | asyncio.StreamReaderProtocol | None,
        request_number: int = 1,
        head: bytes | None = None,
    ) -> bool:
        try:
            # Read the whole request head (request line + headers) in one shot:
            # one readuntil/await beats one await + one buffer scan per header line.
            # Bounded by the timeout so a slow/idle client can't pin the loop forever.
            if head is None:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._timeout)
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
            TimeoutError,
        ):
            return False  # close, EOF, over-long head, or slowloris timeout
        # The complete request head is the admission boundary.  Idle transports
        # are closed by ``_Drain.begin``; a head that won that race is allowed to
        # finish, while no later keep-alive request reaches the application.
        if self._drain is not None and self._drain.draining:
            return False
        if self._connection is not None:
            self._connection.phase = "http"
        request_line, _, rest = head.partition(b"\r\n")
        fields = request_line.decode("latin-1").split()
        if len(fields) != 3:
            return False
        method, raw_path, version = fields
        headers: list[tuple[bytes, bytes]] = []
        header_map: dict[bytes, bytes] = {}
        host_count = 0
        host = b""
        field_count = 0
        field_lines = rest.split(b"\r\n")
        # Per-line matching avoids a whole-block scan for ordinary small heads;
        # one possessive compiled scan avoids a Python/regex crossing per field
        # on large cookie/proxy blocks. The split has two trailing empty entries.
        bulk_validated = len(field_lines) > _PER_FIELD_VALIDATION_LIMIT + 2
        if bulk_validated and _FIELD_BLOCK_MATCH(rest) is None:
            await self._reject_head(writer, HTTPStatus.BAD_REQUEST)
            return False
        for line in field_lines:
            if not line:
                continue
            field_count += 1
            if field_count > 100:
                await self._reject_head(writer, HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
                return False
            name, _, value = line.partition(b":")
            key, val = name.lower(), value.strip(b" \t")
            # Host already takes the stricter authority path below. Validate
            # every other field with the shared RFC 9112 grammar while the
            # specialized parser is already walking its byte lines.
            if not bulk_validated and key != b"host" and _FIELD_BYTES_MATCH(line) is None:
                await self._reject_head(writer, HTTPStatus.BAD_REQUEST)
                return False
            headers.append((key, val))
            header_map[key] = val
            if key == b"host":
                host_count += 1
                host = val
        if version != "HTTP/1.0" and (host_count != 1 or not _valid_host(host)):
            await self._reject_head(writer, HTTPStatus.BAD_REQUEST)
            return False
        try:
            plan = _body.parse_framing(
                [value.decode("latin-1") for name, value in headers if name == b"content-length"],
                [
                    value.decode("latin-1")
                    for name, value in headers
                    if name == b"transfer-encoding"
                ],
                max_size=self._max_body,
                allow_chunked=True,
            )
        except _body.FramingError as exc:
            reason = HTTPStatus(exc.status).phrase
            writer.write(
                f"HTTP/1.1 {exc.status} {reason}\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
            )
            if self._write_timeout is None:
                await writer.drain()
            else:
                await _write.drain(writer, self._write_timeout)
            return False
        if self._credential is not None:  # --auth gates both HTTP and WebSocket
            authz = header_map.get(b"authorization", b"").decode("latin-1")
            if not self._credential.check_header(authz):
                writer.write(_http1.UNAUTHORIZED)
                if self._write_timeout is None:
                    await writer.drain()
                else:
                    await _write.drain(writer, self._write_timeout)
                return False
        if header_map.get(b"upgrade", b"").lower() == b"websocket":
            if plan.chunked or (plan.length or 0):
                return False
            if self._connection is not None:
                self._connection.phase = "websocket"
            await self._serve_websocket(reader, writer, raw_path, headers, header_map)
            return False  # the WebSocket owns the connection until it closes
        keep_alive = _wants_keep_alive(version, header_map) and not (
            self._max_requests and request_number >= self._max_requests
        )
        te = header_map.get(b"te")
        allow_trailers = (
            version == "HTTP/1.1"
            and te is not None
            and any(
                token.strip().lower() == b"trailers"
                for name, value in headers
                if name == b"te"
                for token in value.split(b",")
            )
        )
        body_timeout = self._request_body_timeout
        if body_timeout is None or (not plan.chunked and not plan.length):
            body_stream = _AsyncBody(
                reader,
                plan,
                max_body=self._max_body,
                timeout=self._timeout,
                disconnected=disconnected,
            )
        else:
            body_stream = _TimedAsyncBody(
                reader,
                plan,
                max_body=self._max_body,
                timeout=self._timeout,
                body_timeout=body_timeout,
                disconnected=disconnected,
            )
        path, _, query = raw_path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": version.split("/", 1)[1] if "/" in version else "1.1",
            "method": method,
            "scheme": self._scheme,
            "path": path,
            "raw_path": raw_path.encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "headers": headers,
            "extensions": {"http.response.trailers": {}},
            "server": list(self._server_addr),
            "client": list(writer.get_extra_info("peername", ("", 0))[:2]),
        }
        if self._lifespan_state is not None:
            scope["state"] = self._lifespan_state.copy()
        state = _ResponseState(
            writer,
            method,
            keep_alive,
            self._policy,
            body_stream,
            self._write_timeout,
            allow_trailers=allow_trailers,
        )
        body_stream._response = state

        try:
            await self._app(scope, body_stream.receive, state.send)
            if state._body_complete is not None:
                _log.logger.error(
                    'ASGI app returned without completing its response: %s "%s %s"',
                    scope["client"][0],
                    method,
                    raw_path,
                )
                if not state.started:
                    with contextlib.suppress(OSError):
                        writer.write(_http1.INTERNAL_ERROR)
                with contextlib.suppress(OSError, TimeoutError):
                    if self._write_timeout is None:
                        await writer.drain()
                    else:
                        await _write.drain(writer, self._write_timeout)
                return False
        except _BodyReadError as exc:
            if not state.started:
                reason = HTTPStatus(exc.status).phrase
                writer.write(
                    f"HTTP/1.1 {exc.status} {reason}\r\nContent-Length: 0\r\n"
                    "Connection: close\r\n\r\n".encode("latin-1")
                )
                if self._write_timeout is None:
                    await writer.drain()
                else:
                    await _write.drain(writer, self._write_timeout)
            return False
        except ClientDisconnectError:
            # ASGI 2.4 makes this an application-visible OSError. If the app
            # does not catch it, treat it as an expected lifecycle signal, not
            # an application fault. A fully framed response may still leave a
            # reusable physical connection with a later pipelined request.
            return keep_alive and not state.close and body_stream.complete
        except TimeoutError:
            writer.transport.abort()
            return False
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ConnectionError,
        ):
            return False
        except Exception:
            # The app raised out of its coroutine (it didn't handle its own error).
            # Log with traceback; send a 500 if we haven't committed a response yet.
            _log.logger.error(
                'ASGI app error: %s "%s %s"', scope["client"][0], method, raw_path, exc_info=True
            )
            if not state.started:
                with contextlib.suppress(OSError):
                    writer.write(_http1.INTERNAL_ERROR)
            with contextlib.suppress(OSError, TimeoutError):
                if self._write_timeout is None:
                    await writer.drain()
                else:
                    await _write.drain(writer, self._write_timeout)
            return False
        if self._write_timeout is None:
            await writer.drain()
        else:
            await _write.drain(writer, self._write_timeout)
        if _log.logger.isEnabledFor(logging.INFO):
            _log.logger.info(
                '%s "%s %s %s" %s', scope["client"][0], method, raw_path, version, state.status
            )
        if self._connection is not None:
            self._connection.phase = "idle"
        return (
            keep_alive
            and not state.close
            and body_stream.complete
            and not (self._drain is not None and self._drain.draining)
        )

    async def _reject_head(self, writer: asyncio.StreamWriter, status: HTTPStatus) -> None:
        writer.write(
            f"HTTP/1.1 {status.value} {status.phrase}\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n".encode("latin-1")
        )
        if self._write_timeout is None:
            await writer.drain()
        else:
            await _write.drain(writer, self._write_timeout)

    async def _serve_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        raw_path: str,
        headers: list[tuple[bytes, bytes]],
        header_map: dict[bytes, bytes],
    ) -> None:
        key = header_map.get(b"sec-websocket-key", b"")
        if not key or header_map.get(b"sec-websocket-version", b"") != b"13":
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            if self._write_timeout is None:
                await writer.drain()
            else:
                await _write.drain(writer, self._write_timeout)
            return
        path, _, query = raw_path.partition("?")
        subprotocols = [
            p.strip().decode("latin-1")
            for p in header_map.get(b"sec-websocket-protocol", b"").split(b",")
            if p.strip()
        ]
        client = list(writer.get_extra_info("peername", ("", 0))[:2])
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "wss" if self._scheme == "https" else "ws",
            "path": path,
            "raw_path": raw_path.encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "headers": headers,
            "subprotocols": subprotocols,
            "server": list(self._server_addr),
            "client": client,
        }
        if self._lifespan_state is not None:
            scope["state"] = self._lifespan_state.copy()
        _log.logger.info('%s "WEBSOCKET %s"', client[0], raw_path)
        try:
            await _websocket.serve(
                reader,
                writer,
                scope,
                self._app,
                key,
                write_timeout=self._write_timeout,
                shutdown=None if self._drain is None else self._drain.event,
            )
        except TimeoutError:
            writer.transport.abort()
        except (*_tls.CLIENT_TRANSPORT_ERRORS, asyncio.IncompleteReadError):
            pass
        except Exception:
            _log.logger.error('WebSocket app error: "%s"', raw_path, exc_info=True)


class _ResponseState:
    __slots__ = (
        "_body_complete",
        "_policy",
        "_remaining",
        "_trailer_state",
        "_write_timeout",
        "_writer",
        "chunked",
        "close",
        "headers",
        "started",
        "status",
        "suppress_body",
    )

    def __init__(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        keep_alive: bool,
        policy: list[tuple[bytes, bytes]] | None = None,
        body_complete: Callable[[], bool] | bool = True,
        write_timeout: float | None = None,
        *,
        allow_trailers: bool = False,
    ) -> None:
        self._writer = writer
        self.suppress_body = method == "HEAD"
        self.close = not keep_alive
        self.status: int | None = None
        self.headers: list[Any] | tuple[bytes, bytes] = []
        self.started = False
        self.chunked = False
        self._trailer_state = 1 if allow_trailers else 0
        self._remaining: int | None = None
        self._policy = policy or []
        self._body_complete = body_complete
        self._write_timeout = write_timeout

    @property
    def response_complete(self) -> bool:
        return self._body_complete is None

    async def send(self, event: dict[str, Any]) -> None:
        if self._body_complete is None or self._writer.is_closing():
            raise ClientDisconnectError("ASGI response connection is closed")
        try:
            kind = event["type"]
            if kind == "http.response.start":
                if self.status is not None:
                    raise RuntimeError("ASGI response start sent more than once")
                status = event["status"]
                if (
                    isinstance(status, bool)
                    or not isinstance(status, int)
                    or not 100 <= status <= 999
                ):
                    raise ValueError("ASGI response status must be an integer from 100 to 999")
                self.status = status
                self.headers = list(event.get("headers", ()))
                if event.get("trailers", False):
                    self._trailer_state |= 2
                if not self._trailer_state & 2 and len(self.headers) == 2:
                    first, second = self.headers
                    if (
                        isinstance(first, (tuple, list))
                        and len(first) == 2
                        and isinstance(second, (tuple, list))
                        and len(second) == 2
                        and first[0] == b"content-type"
                        and isinstance(first[1], bytes)
                        and (
                            first[1] == _COMMON_CONTENT_TYPE
                            or len(first[1].translate(None, _FIELD_VALUE_FORBIDDEN))
                            == len(first[1])
                        )
                        and second[0] == b"content-length"
                        and isinstance(second[1], bytes)
                        and second[1].isdigit()
                        and len(second[1]) <= 20
                    ):
                        self._remaining = int(second[1])
                        # ``event["headers"]`` is always materialized as a list.
                        # The tuple is therefore an unambiguous internal marker
                        # for the validated common response shape.
                        self.headers = (
                            b"content-type: " + first[1],
                            b"content-length: " + second[1],
                        )
            elif kind == "http.response.body":
                if not self.started:
                    if self.status is None:
                        raise RuntimeError("ASGI response body sent before response start")
                    self._write_headers()
                    self.started = True
                data = event.get("body", b"")
                if not isinstance(data, bytes):
                    raise TypeError("ASGI response body must be bytes")
                remaining = self._remaining
                if remaining == -1:
                    raise RuntimeError("ASGI response body sent after its final event")
                more_body = event.get("more_body", False)
                if not self.suppress_body and remaining is not None:
                    if not more_body:
                        if len(data) != remaining:
                            mismatch = "exceeds" if len(data) > remaining else "does not match"
                            raise RuntimeError(f"ASGI response body {mismatch} Content-Length")
                        self._remaining = 0
                    else:
                        remaining -= len(data)
                        if remaining < 0:
                            raise RuntimeError("ASGI response body exceeds Content-Length")
                        self._remaining = remaining
                if data and not self.suppress_body:
                    self._writer.write(_http1.chunk(data) if self.chunked else data)
                if not more_body:
                    if self.chunked:
                        self._writer.write(
                            b"0\r\n"
                            if self._trailer_state & 2 and self._wire_trailers
                            else _http1.CHUNK_TERMINATOR
                        )
                    if self._trailer_state & 2:
                        self._remaining = -1
                        if self._write_timeout is None:
                            await self._writer.drain()
                        else:
                            await _write.drain(self._writer, self._write_timeout)
                        return
                    completion = self._body_complete
                    self._body_complete = None
                    if type(completion) is _ResponseCompletion:
                        completion.wake()
                    return
                if self._write_timeout is None:
                    await self._writer.drain()
                else:
                    await _write.drain(self._writer, self._write_timeout)
            elif kind == "http.response.trailers":
                if not self._trailer_state & 2 or self._remaining != -1:
                    raise RuntimeError("ASGI response trailers sent outside the trailer phase")
                headers = _response_trailers(event.get("headers", ()))
                if self._wire_trailers:
                    for name, value in headers:
                        self._writer.write(name + b": " + value + b"\r\n")
                if not event.get("more_trailers", False):
                    if self._wire_trailers:
                        self._writer.write(b"\r\n")
                    self._complete_response()
                    return
                if self._write_timeout is None:
                    await self._writer.drain()
                else:
                    await _write.drain(self._writer, self._write_timeout)
            else:
                raise RuntimeError(f"Unexpected ASGI response event {kind!r}")
        except TimeoutError:
            raise
        except OSError as exc:
            raise ClientDisconnectError("ASGI response peer disconnected") from exc

    @property
    def _wire_trailers(self) -> bool:
        return self._trailer_state & 3 == 3 and self.chunked and not self.suppress_body

    def _complete_response(self) -> None:
        completion = self._body_complete
        self._body_complete = None
        if type(completion) is _ResponseCompletion:
            completion.wake()

    def subscribe_completion(self) -> asyncio.Future[None]:
        completion = self._body_complete
        if completion is None:
            waiter = asyncio.get_running_loop().create_future()
            waiter.set_result(None)
            return waiter
        if not isinstance(completion, _ResponseCompletion):
            body_complete = completion if callable(completion) else lambda: bool(completion)
            completion = _ResponseCompletion(body_complete)
            self._body_complete = completion
        return completion.subscribe()

    def unsubscribe_completion(self, waiter: asyncio.Future[None]) -> None:
        completion = self._body_complete
        if not isinstance(completion, _ResponseCompletion):
            return
        completion.unsubscribe(waiter)
        if not completion.waiters:
            self._body_complete = completion.body_complete

    def _write_headers(self) -> None:
        complete = self._body_complete() if callable(self._body_complete) else self._body_complete
        if not complete:
            self.close = True
        status = self.status
        if status is None:  # pragma: no cover - guarded by send state machine
            raise RuntimeError("ASGI response has no status")
        self.suppress_body = (
            self.suppress_body or status == 204 or status == 304 or 100 <= status < 200
        )
        if self.suppress_body:
            self._remaining = None
        prepared = isinstance(self.headers, tuple)
        generic_lines: list[bytes] = []
        response_lines = self.headers if prepared else generic_lines
        present_set: set[bytes] | None = None if prepared else set()
        present: set[bytes] | frozenset[bytes] = (
            present_set if present_set is not None else _COMMON_RESPONSE_PRESENT
        )
        length: bytes | None = None
        wire_trailers = self._trailer_state & 3 == 3 and not self.suppress_body
        if not prepared:
            for item in self.headers:
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    raise ValueError("ASGI response headers must contain name/value pairs")
                name, value = item
                if not isinstance(name, bytes) or not isinstance(value, bytes):
                    raise TypeError("ASGI response header names and values must be bytes")
                if name != name.lower() or _FIELD_BYTES_MATCH(name + b":" + value) is None:
                    raise ValueError("Invalid ASGI response header field")
                if name == b"transfer-encoding":
                    continue  # protocol server owns wire framing
                if name == b"connection":
                    if b"close" in {token.strip().lower() for token in value.split(b",")}:
                        self.close = True
                    continue
                duplicate_length = False
                wire_value = value
                if name == b"content-length":
                    tokens = [token.strip() for token in value.split(b",")]
                    if (
                        not tokens
                        or any(
                            not token or not token.isdigit() or len(token) > 20 for token in tokens
                        )
                        or len(set(tokens)) != 1
                        or (length is not None and length != tokens[0])
                    ):
                        raise ValueError("Invalid ASGI response Content-Length")
                    duplicate_length = length is not None
                    length = tokens[0]
                    wire_value = length
                if present_set is None:  # pragma: no cover - generic-path invariant
                    raise RuntimeError("ASGI response header state is inconsistent")
                present_set.add(name)
                if not duplicate_length and not (wire_trailers and name == b"content-length"):
                    # Normalize accepted comma-joined/repeated Content-Length
                    # values to one canonical field on the wire.
                    generic_lines.append(name + b": " + wire_value)
            if length is not None and not self.suppress_body:
                self._remaining = int(length)
        if status == 200:
            lines = [b"HTTP/1.1 200 OK"]
        else:
            try:
                reason = HTTPStatus(status).phrase
            except ValueError:
                reason = ""
            lines = [f"HTTP/1.1 {status} {reason}".encode("latin-1")]
        if wire_trailers:
            if present_set is not None:
                present_set.discard(b"content-length")
            self.chunked = True
        lines += response_lines
        for name, value in self._policy:  # servery security/CORS headers, if app didn't set one
            if name.lower() not in present:
                lines.append(name + b": " + value)
        if b"date" not in present:  # origin servers MUST send Date (RFC 7231 §7.1.1.2)
            lines.append(b"Date: " + _http1.http_date().encode("latin-1"))
        if b"server" not in present:
            lines.append(_SERVER_HEADER)
        if self.chunked:
            lines.append(b"Transfer-Encoding: chunked")
        elif b"content-length" in present or self.suppress_body:
            pass
        elif not self.close:
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
    prepared: Any = None,
    start: asyncio.Event | None = None,
    started: Any = None,
    stop: asyncio.Event | None = None,
    listener: socket.socket | None = None,
) -> None:
    """Run ASGI, optionally preparing a listener before admission is committed."""
    app = load_app(config.asgi_app)
    ssl_context = _tls.build_context(config, ["http/1.1"]) if config.uses_tls else None
    scheme = "https" if config.uses_tls else "http"
    credential = auth.parse(config.auth)
    policy = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in _http1.policy_headers(
            security_headers=config.security_headers, cors=config.cors, tls=config.uses_tls
        )
    ]
    lifespan: _Lifespan | None = None
    lifespan_state: dict[str, Any] | None = None
    if config.lifespan != "off":
        lifespan = _Lifespan(
            app,
            mode=config.lifespan,
            timeout=config.lifespan_timeout,
        )
        if await lifespan.startup():
            lifespan_state = lifespan.state
    connections = (
        asyncio.Semaphore(config.max_connections) if config.max_connections is not None else None
    )
    drain = _Drain()

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # asyncio normally applies this itself, but its stdlib guard skips TCP
        # sockets created with protocol 0. Parent-owned and caller-supplied
        # listeners commonly have that shape, unlike getaddrinfo-created ones.
        _set_tcp_nodelay(writer)
        connection = drain.register(writer)
        if connection is None:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if connections is not None:
            # The event loop cannot switch between this check and an immediately
            # successful acquire, so saturated sockets are rejected instead of
            # accumulating an unbounded queue of waiting tasks.
            if connections.locked():
                writer.close()
                await writer.wait_closed()
                drain.unregister(connection)
                return
            await connections.acquire()
        protocol = writer.transport.get_protocol()
        disconnected = protocol if isinstance(protocol, asyncio.StreamReaderProtocol) else None
        try:
            await _Exchange(
                app,
                server.sockets[0].getsockname()[:2],
                scheme,
                credential,
                config.timeout,
                config.keepalive_timeout,
                config.request_head_timeout,
                config.request_body_timeout,
                config.write_timeout,
                config.max_request_body,
                config.max_requests_per_connection,
                policy,
                lifespan_state,
                drain,
                connection,
            ).handle_connection(
                reader,
                writer,
                disconnected,
            )
        finally:
            if connections is not None:
                connections.release()
            drain.unregister(connection)

    adopted: socket.socket | None = None
    try:
        if listener is not None:
            adopted = _listener.adopt_tcp_listener(listener, host=config.host)
            # ``asyncio.start_server(..., sock=...)`` takes ownership of a
            # caller-prepared socket but does not make it non-blocking.  That
            # distinction is critical when several worker event loops share a
            # parent-owned listener: two loops may wake for one connection and
            # the losing loop must receive EAGAIN rather than block in accept().
            adopted.setblocking(False)
            server = await asyncio.start_server(
                connected,
                ssl=ssl_context,
                sock=adopted,
                start_serving=start is None,
            )
        else:
            server = await asyncio.start_server(
                connected,
                config.host,
                config.port,
                ssl=ssl_context,
                start_serving=start is None,
            )
    except BaseException:
        if adopted is not None:
            adopted.close()
        if lifespan is not None:
            with contextlib.suppress(LifespanError):
                await lifespan.shutdown()
        raise
    try:
        address = server.sockets[0].getsockname()
        if prepared is not None:
            prepared(address)
        if start is not None:
            start_waiter = asyncio.create_task(start.wait())
            stop_waiter = asyncio.create_task(stop.wait()) if stop is not None else None
            waiters = {start_waiter} if stop_waiter is None else {start_waiter, stop_waiter}
            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for waiter in pending:
                waiter.cancel()
            for waiter in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            if stop_waiter is not None and stop_waiter in done and not start.is_set():
                return
            await server.start_serving()
        if started is not None:
            started(address)
        if stop is not None:
            await stop.wait()
        else:  # pragma: no cover - CLI runs until interrupted
            await server.serve_forever()
    finally:
        # Closing the listener is deliberately separate from wait_closed(): on
        # current Python versions wait_closed may wait for client callbacks, and
        # those callbacks are exactly the work governed by the drain deadline.
        server.close()
        drain.begin()
        await drain.finish(config.drain_timeout)
        await server.wait_closed()
        if lifespan is not None:
            await lifespan.shutdown()


def run(config: Any) -> None:  # pragma: no cover - blocking CLI entry
    """Run the ASGI server until interrupted (CLI entry)."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_forever(config))

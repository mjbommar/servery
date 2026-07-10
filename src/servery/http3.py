"""Optional HTTP/3 backend via ``aioquic`` (``pip install servery[http3]``).

HTTP/3 runs over QUIC, which needs AEAD packet protection and a TLS-1.3-in-QUIC
handshake — neither is in the standard library, so HTTP/3 cannot be pure-stdlib
(see ``docs/TRANSPORTS.md``). servery's *core* stays zero-dependency; HTTP/3 is an
opt-in extra backed by the well-maintained reference QUIC stack, ``aioquic``.

The request-resolution helpers here are pure-stdlib and reuse servery's
path-safety and listing; only :func:`serve_http3` needs aioquic, imported lazily
so this module (and the rest of servery) import cleanly without it.

A fully native, zero-dependency HTTP/3 (binding the OS OpenSSL ≥3.5 QUIC server
via ctypes — see :mod:`servery._oscrypto` for the proven AEAD foundation) is
plausible future work but a large separate effort.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from servery import _body, _compress, _log, _response, _tls, auth, security

if TYPE_CHECKING:
    from servery.config import Config

H3_ALPN = ["h3"]
_HeaderList = list[tuple[bytes, bytes]]


def _read_file_chunk(handle: BinaryIO, size: int) -> bytes:
    return handle.read(size)


class Http3UnavailableError(RuntimeError):
    """The optional aioquic dependency is not installed."""


def build_response(
    config: Config,
    root_real: str,
    method: str,
    url_path: str,
    accept_encoding: str = "",
    *,
    if_none_match: str | None = None,
    if_modified_since: str | None = None,
    compression_cache: _compress.CompressionCache | None = None,
) -> tuple[int, _HeaderList, _response.ResponseBody]:
    """Resolve a GET/HEAD request to (status, response headers, body).

    This is a deliberately reduced backend versus the HTTP/1.1 handler: it enforces
    auth (in ``_reply``), path-safety, the security/cache headers, ETag +
    conditional 304, but does NOT yet implement Range/206, SPA fallback, index-file
    lookup or ``?download``/``?archive``. Small responses retain the buffered fast
    path; larger files are represented as :class:`~servery._response.FileBody` and
    streamed by the QUIC backend. HTTP/3 remains an opt-in transport extra.

    The dir-or-file body building, content-coding, security headers, and conditional
    handling are shared with HTTP/2 via :mod:`servery._response`, so they can't drift.
    """
    if method not in {"GET", "HEAD"}:
        return 405, [(b"allow", b"GET, HEAD")], b"405"
    fs_path = security.safe_join(root_real, url_path)
    display = url_path.split("?", 1)[0].split("#", 1)[0]
    # safe_join returns None for an escaping path; build_static maps "" to a 404.
    # HTTP/3 is always TLS, so HSTS always applies.
    return _response.build_static(
        config,
        fs_path or "",
        display,
        accept_encoding,
        tls=True,
        if_none_match=if_none_match,
        if_modified_since=if_modified_since,
        compression_cache=compression_cache,
    )


def serve_http3(  # pragma: no cover - requires aioquic + UDP
    config: Config,
    *,
    stop: threading.Event | None = None,
    started: threading.Event | None = None,
    bound_port: list[int] | None = None,
    compression_cache: _compress.CompressionCache | None = None,
) -> None:
    """Run an HTTP/3 server, optionally controlled by a background-thread handle."""
    try:
        import asyncio

        serve_fn: Any = vars(importlib.import_module("aioquic.asyncio"))["serve"]
        quic_protocol_base: Any = vars(importlib.import_module("aioquic.asyncio.protocol"))[
            "QuicConnectionProtocol"
        ]
        h3_connection_cls: Any = vars(importlib.import_module("aioquic.h3.connection"))[
            "H3Connection"
        ]
        h3_events = vars(importlib.import_module("aioquic.h3.events"))
        data_received_cls: Any = h3_events["DataReceived"]
        headers_received_cls: Any = h3_events["HeadersReceived"]
        quic_configuration_cls: Any = vars(importlib.import_module("aioquic.quic.configuration"))[
            "QuicConfiguration"
        ]
    except ImportError as exc:
        raise Http3UnavailableError(
            "HTTP/3 requires the optional aioquic dependency: pip install 'servery[http3]'"
        ) from exc

    if not config.tls_cert and not config.tls_self_signed:
        raise Http3UnavailableError(
            "HTTP/3 (QUIC) requires --tls-cert, --tls-self-signed, or resolved ACME material"
        )

    root_real = os.path.realpath(config.directory)
    credential = auth.parse(config.auth)
    response_cache = compression_cache or _compress.CompressionCache(config.compression_cache_size)
    active_connections = 0

    class _Protocol(quic_protocol_base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal active_connections
            super().__init__(*args, **kwargs)
            self._http = h3_connection_cls(self._quic)
            self._requests: dict[int, dict[bytes, bytes]] = {}
            self._request_sizes: dict[int, int] = {}
            self._request_expected: dict[int, int | None] = {}
            self._rejected_requests: set[int] = set()
            self._tasks: set[asyncio.Task[None]] = set()
            self._admitted = (
                config.max_connections is None or active_connections < config.max_connections
            )
            if self._admitted:
                active_connections += 1
            else:
                self.close(error_code=0x0107, reason_phrase="connection limit reached")

        def connection_lost(self, exc: Exception | None) -> None:
            nonlocal active_connections
            for task in self._tasks:
                task.cancel()
            if self._admitted:
                active_connections -= 1
                self._admitted = False
            super().connection_lost(exc)

        def quic_event_received(self, event: Any) -> None:
            if not self._admitted:
                return
            for h3_event in self._http.handle_event(event):
                if isinstance(h3_event, headers_received_cls):
                    if h3_event.stream_id in self._requests:  # trailing fields
                        if h3_event.stream_ended:
                            self._schedule_reply(h3_event.stream_id)
                        continue
                    lengths = [
                        value.decode("latin-1")
                        for name, value in h3_event.headers
                        if name == b"content-length"
                    ]
                    transfer = [
                        value for name, value in h3_event.headers if name == b"transfer-encoding"
                    ]
                    if transfer:
                        self._reject_request(h3_event.stream_id, 400)
                        continue
                    try:
                        _body.parse_framing(lengths, [], max_size=config.max_request_body)
                    except _body.FramingError as exc:
                        self._reject_request(h3_event.stream_id, exc.status)
                        continue
                    self._requests[h3_event.stream_id] = dict(h3_event.headers)
                    self._request_sizes[h3_event.stream_id] = 0
                    self._request_expected[h3_event.stream_id] = (
                        int(lengths[0]) if lengths else None
                    )
                    if h3_event.stream_ended:
                        self._schedule_reply(h3_event.stream_id)
                elif isinstance(h3_event, data_received_cls):
                    if h3_event.stream_id in self._rejected_requests:
                        if h3_event.stream_ended:
                            self._rejected_requests.discard(h3_event.stream_id)
                        continue
                    size = self._request_sizes.get(h3_event.stream_id, 0) + len(h3_event.data)
                    self._request_sizes[h3_event.stream_id] = size
                    if size > config.max_request_body:
                        self._reject_request(h3_event.stream_id, 413)
                    elif h3_event.stream_ended:
                        self._schedule_reply(h3_event.stream_id)

        def _reject_request(self, stream_id: int, status: int) -> None:
            self._requests.pop(stream_id, None)
            self._request_sizes.pop(stream_id, None)
            self._request_expected.pop(stream_id, None)
            self._rejected_requests.add(stream_id)
            self._http.send_headers(
                stream_id,
                [(b":status", str(status).encode("ascii")), (b"content-length", b"0")],
                end_stream=True,
            )
            self.transmit()

        def _schedule_reply(self, stream_id: int) -> None:
            headers = self._requests.get(stream_id)
            if headers is None:
                return
            expected = self._request_expected.get(stream_id)
            if expected is not None and expected != self._request_sizes.get(stream_id, 0):
                self._reject_request(stream_id, 400)
                return
            self._requests.pop(stream_id, None)
            self._request_sizes.pop(stream_id, None)
            self._request_expected.pop(stream_id, None)
            task = asyncio.create_task(self._reply(stream_id, headers))
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

        def _task_done(self, task: asyncio.Task[None]) -> None:
            self._tasks.discard(task)
            if task.cancelled():
                return
            if error := task.exception():
                _log.logger.error(
                    "HTTP/3 response task failed: %r",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        async def _reply(self, stream_id: int, headers: dict[bytes, bytes]) -> None:
            method = headers.get(b":method", b"").decode("latin-1")
            path = headers.get(b":path", b"/").decode("latin-1")
            if credential is not None:  # --auth gates HTTP/3 too
                authz = headers.get(b"authorization", b"").decode("latin-1")
                if not credential.check_header(authz):
                    self._http.send_headers(
                        stream_id,
                        [
                            (b":status", b"401"),
                            (b"www-authenticate", auth.WWW_AUTHENTICATE.encode("latin-1")),
                        ],
                        end_stream=True,
                    )
                    self.transmit()
                    _log.logger.info('HTTP/3 "%s %s" 401', method, path)
                    return
            accept = headers.get(b"accept-encoding", b"").decode("latin-1")
            inm = headers.get(b"if-none-match")
            ims = headers.get(b"if-modified-since")
            status, response_headers, body = await asyncio.to_thread(
                build_response,
                config,
                root_real,
                method,
                path,
                accept,
                if_none_match=inm.decode("latin-1") if inm is not None else None,
                if_modified_since=ims.decode("latin-1") if ims is not None else None,
                compression_cache=response_cache,
            )
            _log.logger.info('HTTP/3 "%s %s" %s', method, path, status)
            body_size = len(body) if isinstance(body, bytes) else body.size
            send_body = method != "HEAD" and body_size > 0
            self._http.send_headers(
                stream_id,
                [(b":status", str(status).encode("ascii")), *response_headers],
                end_stream=not send_body,
            )
            if send_body:
                if isinstance(body, bytes):
                    self._http.send_data(stream_id, body, end_stream=True)
                else:
                    await self._stream_file(stream_id, body)
            self.transmit()

        async def _stream_file(self, stream_id: int, body: _response.FileBody) -> None:
            remaining = body.size
            handle = cast("BinaryIO", await asyncio.to_thread(Path(body.path).open, "rb"))
            try:
                while remaining:
                    await self._wait_for_send_capacity(stream_id)
                    chunk = await asyncio.to_thread(
                        _read_file_chunk, handle, min(64 * 1024, remaining)
                    )
                    if not chunk:
                        self._quic.reset_stream(stream_id, 0x0102)
                        return
                    remaining -= len(chunk)
                    self._http.send_data(stream_id, chunk, end_stream=remaining == 0)
                    self.transmit()
                    await asyncio.sleep(0)
            finally:
                await asyncio.to_thread(handle.close)

        async def _wait_for_send_capacity(self, stream_id: int) -> None:
            # aioquic's public H3 API intentionally has no async drain() method.
            # Bound its private per-stream sender buffer here, isolated to this
            # optional backend, so a flow-control-stalled peer cannot queue a file.
            limit = 256 * 1024
            while self._queued_bytes(stream_id) >= limit:
                self.transmit()
                await asyncio.sleep(0.005)

        def _queued_bytes(self, stream_id: int) -> int:
            streams = getattr(self._quic, "_streams", {})
            stream = streams.get(stream_id)
            sender = getattr(stream, "sender", None)
            buffer = getattr(sender, "_buffer", b"")
            return len(buffer)

    async def _run() -> None:
        configuration = quic_configuration_cls(is_client=False, alpn_protocols=H3_ALPN)
        cert_context = (
            _tls.self_signed_files(config)
            if config.tls_cert is None
            else contextlib.nullcontext((config.tls_cert, config.tls_key))
        )
        with cert_context as (cert_path, key_path):
            configuration.load_cert_chain(cert_path, key_path, config.tls_password)
            port = config.http3_port if config.http3_port is not None else config.port
            server = await serve_fn(
                config.host, port, configuration=configuration, create_protocol=_Protocol
            )
        transport = getattr(server, "_transport", None)
        sockname = transport.get_extra_info("sockname") if transport is not None else None
        actual_port = int(sockname[1]) if sockname else port
        if bound_port is not None:
            bound_port.append(actual_port)
        if started is not None:
            started.set()
        try:
            if stop is None:
                await asyncio.Future()
            else:
                while not stop.is_set():
                    await asyncio.sleep(0.05)
        finally:
            server.close()

    port = config.http3_port if config.http3_port is not None else config.port
    _log.logger.info("servery: serving HTTP/3 (QUIC) on %s:%s", config.host, port)
    asyncio.run(_run())


class Http3ServerHandle:
    """Background HTTP/3 listener owned by the unified server lifecycle."""

    def __init__(
        self,
        config: Config,
        *,
        compression_cache: _compress.CompressionCache | None = None,
    ) -> None:
        self._stop = threading.Event()
        self._started = threading.Event()
        self._errors: list[BaseException] = []
        self.bound_port: list[int] = []

        def run() -> None:
            try:
                serve_http3(
                    config,
                    stop=self._stop,
                    started=self._started,
                    bound_port=self.bound_port,
                    compression_cache=compression_cache,
                )
            except BaseException as exc:
                self._errors.append(exc)
                self._started.set()

        self._thread = threading.Thread(target=run, name="servery-http3", daemon=True)
        self._thread.start()
        if not self._started.wait(10):
            self._stop.set()
            self._thread.join(timeout=2)
            raise Http3UnavailableError("HTTP/3 listener did not start within 10 seconds")
        if self._errors:
            error = self._errors[0]
            if isinstance(error, Exception):
                raise error
            raise Http3UnavailableError("HTTP/3 listener failed during startup") from error

    @property
    def port(self) -> int:
        return self.bound_port[0]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise Http3UnavailableError("HTTP/3 listener did not stop within 5 seconds")

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()


def start_http3(
    config: Config, *, compression_cache: _compress.CompressionCache | None = None
) -> Http3ServerHandle:
    """Start HTTP/3 beside the TCP server and fail synchronously on setup errors."""
    return Http3ServerHandle(config, compression_cache=compression_cache)

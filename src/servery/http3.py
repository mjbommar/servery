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

import os
from typing import TYPE_CHECKING

from servery import _log, _response, auth, security

if TYPE_CHECKING:
    from servery.config import Config

H3_ALPN = ["h3"]
_HeaderList = list[tuple[bytes, bytes]]


class Http3UnavailableError(RuntimeError):
    """The optional aioquic dependency is not installed."""


def build_response(
    config: Config, root_real: str, method: str, url_path: str, accept_encoding: str = ""
) -> tuple[int, _HeaderList, bytes]:
    """Resolve a GET/HEAD request to (status, response headers, body).

    This is a deliberately reduced backend versus the HTTP/1.1 handler: it enforces
    auth (in ``_reply``), path-safety, the security headers, CORS, and cache-control,
    but does NOT yet implement Range/206, conditional/304, SPA fallback, index-file
    lookup, ``?download``/``?archive``, or streaming (it buffers the whole file).
    HTTP/3 is an opt-in experimental extra; the full-featured path is HTTP/1.1.

    The dir-or-file body building + the content-coding/security headers are shared
    with HTTP/2 via :mod:`servery._response`, so the decisions can't drift.
    """
    if method not in {"GET", "HEAD"}:
        return 405, [(b"allow", b"GET, HEAD")], b"405"
    fs_path = security.safe_join(root_real, url_path)
    display = url_path.split("?", 1)[0].split("#", 1)[0]
    # safe_join returns None for an escaping path; build_static maps "" to a 404.
    # HTTP/3 is always TLS, so HSTS always applies.
    return _response.build_static(config, fs_path or "", display, accept_encoding, tls=True)


def serve_http3(config: Config) -> None:  # pragma: no cover - requires aioquic + UDP
    """Run an HTTP/3 server. Requires ``servery[http3]`` (aioquic) and TLS cert/key."""
    try:
        import asyncio

        from aioquic.asyncio import serve  # ty: ignore[unresolved-import]
        from aioquic.asyncio.protocol import QuicConnectionProtocol  # ty: ignore[unresolved-import]
        from aioquic.h3.connection import H3Connection  # ty: ignore[unresolved-import]
        from aioquic.h3.events import DataReceived, HeadersReceived  # ty: ignore[unresolved-import]
        from aioquic.quic.configuration import QuicConfiguration  # ty: ignore[unresolved-import]
    except ImportError as exc:
        raise Http3UnavailableError(
            "HTTP/3 requires the optional aioquic dependency: pip install 'servery[http3]'"
        ) from exc

    if not config.tls_cert or not config.tls_key:
        raise Http3UnavailableError("HTTP/3 (QUIC) requires --tls-cert and --tls-key")

    root_real = os.path.realpath(config.directory)
    credential = auth.parse(config.auth)

    class _Protocol(QuicConnectionProtocol):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._http = H3Connection(self._quic)
            self._requests: dict[int, dict[bytes, bytes]] = {}

        def quic_event_received(self, event: object) -> None:
            for h3_event in self._http.handle_event(event):
                if isinstance(h3_event, HeadersReceived):
                    self._requests[h3_event.stream_id] = dict(h3_event.headers)
                    if h3_event.stream_ended:
                        self._reply(h3_event.stream_id)
                elif isinstance(h3_event, DataReceived) and h3_event.stream_ended:
                    self._reply(h3_event.stream_id)

        def _reply(self, stream_id: int) -> None:
            headers = self._requests.pop(stream_id, {})
            method = headers.get(b":method", b"").decode("latin-1")
            path = headers.get(b":path", b"/").decode("latin-1")
            if credential is not None:  # --auth gates HTTP/3 too
                authz = headers.get(b"authorization", b"").decode("latin-1")
                if not credential.check_header(authz):
                    self._http.send_headers(
                        stream_id,
                        [(b":status", b"401"), (b"www-authenticate", b'Basic realm="servery"')],
                        end_stream=True,
                    )
                    self.transmit()
                    _log.logger.info('HTTP/3 "%s %s" 401', method, path)
                    return
            accept = headers.get(b"accept-encoding", b"").decode("latin-1")
            status, response_headers, body = build_response(config, root_real, method, path, accept)
            _log.logger.info('HTTP/3 "%s %s" %s', method, path, status)
            send_body = body if method != "HEAD" else b""
            self._http.send_headers(
                stream_id,
                [(b":status", str(status).encode("ascii")), *response_headers],
                end_stream=not send_body,
            )
            if send_body:
                self._http.send_data(stream_id, send_body, end_stream=True)
            self.transmit()

    async def _run() -> None:
        configuration = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
        configuration.load_cert_chain(config.tls_cert, config.tls_key, config.tls_password)
        await serve(
            config.host, config.port, configuration=configuration, create_protocol=_Protocol
        )
        await asyncio.Future()

    _log.logger.info("servery: serving HTTP/3 (QUIC) on %s:%s", config.host, config.port)
    asyncio.run(_run())

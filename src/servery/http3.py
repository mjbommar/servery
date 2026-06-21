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

import mimetypes
import os
import urllib.parse
from typing import TYPE_CHECKING

from servery import listing, security

if TYPE_CHECKING:
    from servery.config import Config

H3_ALPN = ["h3"]
_HeaderList = list[tuple[bytes, bytes]]


class Http3UnavailableError(RuntimeError):
    """The optional aioquic dependency is not installed."""


def safe_fs_path(directory: str, root_real: str, url_path: str) -> str | None:
    """Map a URL path to a contained filesystem path, or None if it escapes."""
    path = url_path.split("?", 1)[0].split("#", 1)[0]
    path = urllib.parse.unquote(path)
    parts = [part for part in path.split("/") if part and part not in {".", ".."}]
    candidate = os.path.join(directory, *parts)  # noqa: PTH118 (os-level by design)
    return candidate if security.is_contained(root_real, candidate) else None


def build_response(
    config: Config, root_real: str, method: str, url_path: str
) -> tuple[int, _HeaderList, bytes]:
    """Resolve a GET/HEAD request to (status, response headers, body)."""
    if method not in {"GET", "HEAD"}:
        return 405, [(b"allow", b"GET, HEAD")], b"405"
    fs_path = safe_fs_path(str(config.directory), root_real, url_path)
    if fs_path is None:
        return 404, [(b"content-type", b"text/plain")], b"404"
    display = url_path.split("?", 1)[0].split("#", 1)[0]
    headers: _HeaderList = []
    if config.security_headers:
        headers.append((b"x-content-type-options", b"nosniff"))
    if os.path.isdir(fs_path):  # noqa: PTH112 (os-level by design)
        if not display.endswith("/"):
            return 301, [(b"location", (display + "/").encode("latin-1"))], b""
        try:
            body = listing.render(fs_path, display, show_hidden=config.show_hidden)
        except OSError:
            return 404, [(b"content-type", b"text/plain")], b"404"
        headers.append((b"content-type", b"text/html; charset=utf-8"))
        return 200, headers, body
    try:
        with open(fs_path, "rb") as handle:  # noqa: PTH123 (os-level by design)
            body = handle.read()
    except OSError:
        return 404, [(b"content-type", b"text/plain")], b"404"
    ctype = mimetypes.guess_file_type(fs_path)[0] or "application/octet-stream"
    headers.append((b"content-type", ctype.encode("latin-1")))
    return 200, headers, body


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
            status, response_headers, body = build_response(config, root_real, method, path)
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

    asyncio.run(_run())

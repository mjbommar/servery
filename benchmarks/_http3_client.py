"""aioquic-based HTTP/3 client + server harness for the e2e benchmark.

Imported ONLY after ``pytest.importorskip("aioquic")`` succeeds, so the top-level
aioquic imports never run on the free-threaded interpreter (which can't build it).

``http3_server`` starts servery's ``serve_http3`` on a real UDP port with a temp
self-signed cert; ``http3_client`` opens ONE persistent QUIC connection on a
background event loop and returns a synchronous ``get(path)`` — so the benchmark
times a single request over an established connection, not the QUIC handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import DataReceived
from aioquic.quic.configuration import QuicConfiguration

from servery import _certgen
from servery import http3 as _http3
from servery.config import Config


@contextlib.contextmanager
def http3_server(tree: Path) -> Iterator[tuple[str, int, str]]:
    """Run ``serve_http3`` on an ephemeral UDP port; yield (host, port, cafile)."""
    cert_pem, key_pem = _certgen.generate(["localhost", "127.0.0.1"])
    tmp = Path(tempfile.mkdtemp(prefix="bench-h3-"))
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    cert.write_text(cert_pem)
    key.write_text(key_pem)

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()  # serve_http3 rebinds it (small race, fine on loopback)

    config = Config.create(
        str(tree), host="127.0.0.1", port=port, quiet=True, tls_cert=str(cert), tls_key=str(key)
    )

    def run() -> None:
        with contextlib.suppress(BaseException):
            _http3.serve_http3(config)  # asyncio.run(...) — blocks; no clean stop

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.6)  # let aioquic bind the UDP socket
    try:
        yield "127.0.0.1", port, str(cert)
    finally:
        pass  # serve_http3 has no stop hook; the daemon thread dies with the process


class _H3Client(QuicConnectionProtocol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)
        self._waiters: dict[int, list[Any]] = {}

    def request(self, authority: str, path: str) -> asyncio.Future[bytes]:
        stream_id = self._quic.get_next_available_stream_id()
        fut: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
        self._waiters[stream_id] = [fut, bytearray()]
        self._http.send_headers(
            stream_id,
            [
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", authority.encode()),
                (b":path", path.encode()),
            ],
            end_stream=True,
        )
        self.transmit()
        return fut

    def quic_event_received(self, event: Any) -> None:
        for h3_event in self._http.handle_event(event):
            waiter = self._waiters.get(getattr(h3_event, "stream_id", -1))
            if waiter is None:
                continue
            if isinstance(h3_event, DataReceived):
                waiter[1] += h3_event.data
            if getattr(h3_event, "stream_ended", False) and not waiter[0].done():
                waiter[0].set_result(bytes(waiter[1]))


@contextlib.contextmanager
def http3_client(host: str, port: int, cafile: str) -> Iterator[Callable[[str], bytes]]:
    """Open one persistent QUIC/H3 connection on a background loop; yield get(path)."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box: dict[str, Any] = {}

    config = QuicConfiguration(is_client=True, alpn_protocols=["h3"])
    config.load_verify_locations(cafile)
    authority = f"{host}:{port}"

    async def main() -> None:
        async with connect(host, port, configuration=config, create_protocol=_H3Client) as client:
            await client.wait_connected()
            box["client"] = client
            ready.set()
            await box["stop"].wait()

    def runner() -> None:
        asyncio.set_event_loop(loop)
        box["stop"] = asyncio.Event()
        loop.run_until_complete(main())
        loop.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    if not ready.wait(10):
        raise RuntimeError("HTTP/3 client did not connect")

    def get(path: str) -> bytes:
        fut = asyncio.run_coroutine_threadsafe(_do(path), loop)
        return fut.result(timeout=10)

    async def _do(path: str) -> bytes:
        return await box["client"].request(authority, path)

    try:
        yield get
    finally:
        loop.call_soon_threadsafe(box["stop"].set)
        thread.join(5)

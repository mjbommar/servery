"""Shared benchmark harness: spin each transport up in-process, drive one request.

The context managers bind to an ephemeral port (``port=0``) and yield ``(host, port)``;
``client_get`` builds a persistent keep-alive HTTP/1.1 connection and returns a
zero-argument closure that does one GET round-trip (what pytest-benchmark times).
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import threading
from collections.abc import Callable, Iterator

from servery import asgi as _asgi
from servery.config import Config
from servery.server import make_server


@contextlib.contextmanager
def threaded_server(config: Config) -> Iterator[tuple[str, int]]:
    """Run a ServeryHTTPServer (HTTP/1.1, TLS, H2, WSGI, CGI, proxy) in a thread."""
    httpd = make_server(config)
    host, port = httpd.server_address[0], httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield str(host), int(port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def asgi_server(config: Config) -> Iterator[tuple[str, int]]:
    """Run the asyncio ASGI server in a background event loop; yield (host, port)."""
    holder: dict[str, tuple] = {}
    ready = threading.Event()
    loop = asyncio.new_event_loop()
    box: dict[str, asyncio.Event] = {}

    def runner() -> None:
        asyncio.set_event_loop(loop)
        box["stop"] = asyncio.Event()

        def on_ready(addr: tuple) -> None:
            holder["addr"] = addr
            ready.set()

        try:
            loop.run_until_complete(_asgi.serve_forever(config, started=on_ready, stop=box["stop"]))
        finally:
            loop.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    if not ready.wait(5):
        raise RuntimeError("ASGI server did not start")
    addr = holder["addr"]
    try:
        yield str(addr[0]), int(addr[1])
    finally:
        loop.call_soon_threadsafe(box["stop"].set)
        thread.join(5)


def client_get(
    host: str, port: int, path: str
) -> tuple[http.client.HTTPConnection, Callable[[], bytes]]:
    """Return a persistent keep-alive connection and a closure doing one GET → body."""
    conn = http.client.HTTPConnection(host, port, timeout=30)

    def do() -> bytes:
        conn.request("GET", path)
        return conn.getresponse().read()

    return conn, do

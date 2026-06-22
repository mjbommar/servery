"""ASGI hosting (--asgi) tests — the asyncio server, run in a background loop."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import unittest
from collections.abc import Iterator
from typing import Any

from servery import asgi
from servery.config import Config

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


@contextlib.contextmanager
def serving_asgi(spec: str) -> Iterator[tuple[str, int]]:
    """Run the ASGI server for ``spec`` in a background event loop; yield (host, port)."""
    config = Config.create(".", host="127.0.0.1", port=0, quiet=True, asgi_app=spec)
    holder: dict[str, Any] = {}
    ready = threading.Event()
    loop = asyncio.new_event_loop()
    box: dict[str, asyncio.Event] = {}

    def runner() -> None:
        asyncio.set_event_loop(loop)
        box["stop"] = asyncio.Event()

        def on_ready(addr: tuple[Any, ...]) -> None:
            holder["addr"] = addr
            ready.set()

        try:
            loop.run_until_complete(asgi.serve_forever(config, started=on_ready, stop=box["stop"]))
        finally:
            loop.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    if not ready.wait(5):
        raise RuntimeError("ASGI server did not start")
    addr = holder["addr"]
    try:
        yield str(addr[0]), int(addr[1])  # type: ignore[index]
    finally:
        loop.call_soon_threadsafe(box["stop"].set)
        thread.join(5)


class LoadAppTest(unittest.TestCase):
    def test_loads_and_rejects(self):
        self.assertTrue(callable(asgi.load_app("tests._asgiapp:echo")))
        with self.assertRaises(ValueError):
            asgi.load_app("tests._asgiapp:nope")


class ConfigTest(unittest.TestCase):
    def test_asgi_exclusivity_and_no_tls(self):
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", wsgi_app="m:b")
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", http2=True)
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", tls_self_signed=True)


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGIServerTest(unittest.TestCase):
    def test_methods_and_body(self):
        with serving_asgi("tests._asgiapp:echo") as (host, port), httpx.Client() as client:
            got = client.get(f"http://{host}:{port}/hi?q=1")
            self.assertEqual(got.status_code, 200)
            self.assertEqual(got.text, "asgi GET /hi ")
            posted = client.post(f"http://{host}:{port}/up", content=b"DATA")
            self.assertEqual(posted.text, "asgi POST /up DATA")

    def test_keep_alive_two_requests_one_connection(self):
        # httpx reuses the connection across both requests on a keep-alive server.
        with serving_asgi("tests._asgiapp:echo") as (host, port), httpx.Client() as client:
            self.assertEqual(client.get(f"http://{host}:{port}/a").text, "asgi GET /a ")
            self.assertEqual(client.get(f"http://{host}:{port}/b").text, "asgi GET /b ")

    def test_lifespan_startup_ran(self):
        with serving_asgi("tests._asgiapp:with_lifespan") as (host, port):
            with httpx.Client() as client:
                resp = client.get(f"http://{host}:{port}/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("startup", resp.text)


class ASGIChunkedTest(unittest.TestCase):
    def test_streaming_uses_chunked(self):
        with serving_asgi("tests._asgiapp:streaming") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                sock.settimeout(3)
                data = b""
                while b"0\r\n\r\n" not in data:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
            finally:
                sock.close()
            self.assertIn(b"transfer-encoding: chunked", data.split(b"\r\n\r\n", 1)[0].lower())
            self.assertIn(b"part1", data)
            self.assertIn(b"part2", data)


if __name__ == "__main__":
    unittest.main()

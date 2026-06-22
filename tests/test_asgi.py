"""ASGI hosting (--asgi) tests — the asyncio server, run in a background loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
import unittest
from collections.abc import Iterator
from typing import Any

from servery import asgi
from servery.config import Config
from tests._harness import capturing_logs, wait_for

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


@contextlib.contextmanager
def serving_asgi(
    spec: str, *, tls: bool = False, auth: str | None = None
) -> Iterator[tuple[str, int]]:
    """Run the ASGI server for ``spec`` in a background event loop; yield (host, port)."""
    config = Config.create(
        ".", host="127.0.0.1", port=0, quiet=True, asgi_app=spec, tls_self_signed=tls, auth=auth
    )
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
    def test_asgi_exclusivity(self):
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", wsgi_app="m:b")
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", http2=True)

    def test_asgi_allows_tls(self):
        cfg = Config.create(".", asgi_app="m:a", tls_self_signed=True)
        self.assertTrue(cfg.uses_tls)


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

    def test_chunked_request_body_is_reassembled(self):
        # A client chunked request body (no Content-Length) must reach the app
        # whole — and must not desync the connection (FastAPI streaming uploads).
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(
                    b"POST /p HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n"
                )
                sock.settimeout(3)
                data = b""
                while True:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
            finally:
                sock.close()
            self.assertIn(b"asgi POST /p helloworld", data)


class WebSocketHandshakeTest(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        from servery import _websocket

        # The canonical example from RFC 6455 §1.3.
        self.assertEqual(
            _websocket.accept_key(b"dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


def _ws_open(host: str, port: int, path: str = "/") -> tuple[socket.socket, bytes, bytes]:
    """Open a WebSocket: do the upgrade, return (sock, key, handshake_response)."""
    import base64
    import os

    key = base64.b64encode(os.urandom(16))
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(
        b"GET " + path.encode() + b" HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: " + key + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)
    return sock, key, resp


def _ws_send_text(sock: socket.socket, text: str) -> None:
    import os

    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes((0x81, 0x80 | len(payload))) + mask + masked)  # FIN+text, masked


def _ws_read_text(sock: socket.socket) -> str:
    head = sock.recv(2)
    length = head[1] & 0x7F  # server frames are unmasked; payloads here are small
    data = b""
    while len(data) < length:
        data += sock.recv(length - len(data))
    return data.decode()


class WebSocketWireTest(unittest.TestCase):
    def test_echo_over_the_wire(self):
        from servery import _websocket

        with serving_asgi("tests._asgiapp:ws_echo") as (host, port):
            sock, key, resp = _ws_open(host, port)
            try:
                self.assertIn(b"101 Switching Protocols", resp)
                # base64 accept value is case-sensitive — match it verbatim.
                self.assertIn(_websocket.accept_key(key).encode(), resp)
                _ws_send_text(sock, "hi")
                self.assertEqual(_ws_read_text(sock), "echo:hi")
                _ws_send_text(sock, "again")
                self.assertEqual(_ws_read_text(sock), "echo:again")
            finally:
                sock.close()


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGIAuthTest(unittest.TestCase):
    def test_auth_is_enforced(self):
        with serving_asgi("tests._asgiapp:echo", auth="u:p") as (host, port):
            with httpx.Client() as client:
                self.assertEqual(client.get(f"http://{host}:{port}/x").status_code, 401)
                ok = client.get(f"http://{host}:{port}/x", auth=("u", "p"))
                self.assertEqual(ok.status_code, 200)


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGITLSTest(unittest.TestCase):
    def test_serves_over_https(self):
        with serving_asgi("tests._asgiapp:echo", tls=True) as (host, port):
            with httpx.Client(verify=False) as client:
                resp = client.get(f"https://{host}:{port}/secure?q=1")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.text, "asgi GET /secure ")


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGITelemetryTest(unittest.TestCase):
    def test_app_error_returns_500_and_is_logged(self):
        with (
            capturing_logs(logging.ERROR) as cap,
            serving_asgi("tests._asgiapp:crashing") as (
                host,
                port,
            ),
        ):
            with httpx.Client() as client:
                resp = client.get(f"http://{host}:{port}/x")
            self.assertEqual(resp.status_code, 500)
            self.assertTrue(
                wait_for(lambda: any(r.levelno == logging.ERROR for r in cap.records)),
                "expected an ERROR log for the app crash",
            )
            self.assertTrue(any("ASGI app error" in r.getMessage() for r in cap.records))

    def test_request_is_access_logged(self):
        with (
            capturing_logs(logging.INFO) as cap,
            serving_asgi("tests._asgiapp:echo") as (
                host,
                port,
            ),
        ):
            with httpx.Client() as client:
                client.get(f"http://{host}:{port}/hello?q=1")
            self.assertTrue(
                wait_for(lambda: any('"GET /hello?q=1' in r.getMessage() for r in cap.records)),
                "expected an INFO access-log line",
            )


if __name__ == "__main__":
    unittest.main()

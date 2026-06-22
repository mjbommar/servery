"""Reverse-proxy (--proxy) tests: forwarding, headers, local pass-through."""

from __future__ import annotations

import contextlib
import tempfile
import threading
import unittest
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from servery.config import Config
from tests._harness import serving

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/stream"):
            # No Content-Length (HTTP/1.0 close-delimited upstream).
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"streamed-no-content-length")
            return
        xff = self.headers.get("X-Forwarded-For", "")
        proto = self.headers.get("X-Forwarded-Proto", "")
        body = f"upstream:{self.path} xff={xff} proto={proto}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b"posted:" + data
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base signature)
        return  # silence the upstream's request logging


@contextlib.contextmanager
def upstream() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class ConfigTest(unittest.TestCase):
    def test_bad_specs_rejected(self):
        with self.assertRaises(ValueError):
            Config.create(".", proxy=["noprefix=http://x"])  # prefix must start with /
        with self.assertRaises(ValueError):
            Config.create(".", proxy=["/api=ftp://x"])  # upstream must be http(s)

    def test_longest_prefix_wins(self):
        cfg = Config.create(".", proxy=["/api=http://a", "/api/v2=http://b"])
        self.assertEqual(cfg.proxy_routes[0][0], "/api/v2")  # sorted longest-first

    def test_proxy_incompatible_with_dynamic_handlers(self):
        # The proxy only dispatches on the HTTP/1.1 file handler; combining it with
        # a mode that replaces that handler must error, not silently drop the proxy.
        with self.assertRaises(ValueError):
            Config.create(".", proxy=["/api=http://x"], wsgi_app="m:a")
        with self.assertRaises(ValueError):
            Config.create(".", proxy=["/api=http://x"], http2=True)


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ProxyServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "local.txt").write_text("served locally")
        self._up = upstream()
        self._port = self._up.__enter__()
        self.cfg = Config.create(
            self._tmp.name,
            host="127.0.0.1",
            port=0,
            quiet=True,
            proxy=[f"/api=http://127.0.0.1:{self._port}"],
        )

    def tearDown(self):
        self._up.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_matching_request_is_forwarded(self):
        with serving(self.cfg) as (host, port):
            resp = httpx.get(f"http://{host}:{port}/api/hello?q=1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("upstream:/api/hello?q=1", resp.text)
        self.assertIn("xff=127.0.0.1", resp.text)  # X-Forwarded-For injected
        self.assertIn("proto=http", resp.text)

    def test_auth_gates_proxied_routes(self):
        cfg = Config.create(
            self._tmp.name,
            host="127.0.0.1",
            port=0,
            quiet=True,
            proxy=[f"/api=http://127.0.0.1:{self._port}"],
            auth="u:p",
        )
        with serving(cfg) as (host, port):
            self.assertEqual(httpx.get(f"http://{host}:{port}/api/x").status_code, 401)
            ok = httpx.get(f"http://{host}:{port}/api/x", auth=("u", "p"))
            self.assertEqual(ok.status_code, 200)
            self.assertIn("upstream:", ok.text)

    def test_non_matching_request_served_locally(self):
        with serving(self.cfg) as (host, port):
            resp = httpx.get(f"http://{host}:{port}/local.txt")
        self.assertEqual(resp.text, "served locally")

    def test_post_body_is_forwarded(self):
        with serving(self.cfg) as (host, port):
            resp = httpx.post(f"http://{host}:{port}/api/submit", content=b"PAYLOAD")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.text, "posted:PAYLOAD")

    def test_no_length_upstream_is_chunked_kept_alive_and_dated(self):
        import socket

        # Upstream omits Content-Length -> servery must chunk it (keep-alive),
        # not force Connection: close, and backfill Date/Server. (Regression for
        # the bugs found while unifying the response writers.)
        with serving(self.cfg) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\n\r\n")  # keep-alive
                sock.settimeout(3)
                data = b""
                while b"0\r\n\r\n" not in data:  # read to the chunked terminator
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
            finally:
                sock.close()
            head = data.split(b"\r\n\r\n", 1)[0].lower()
            self.assertIn(b"transfer-encoding: chunked", head)
            self.assertNotIn(b"connection: close", head)
            self.assertIn(b"date:", head)
            self.assertIn(b"server:", head)
            self.assertIn(b"streamed-no-content-length", data)


if __name__ == "__main__":
    unittest.main()

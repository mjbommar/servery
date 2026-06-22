"""WSGI hosting (--wsgi) tests. Apps are wsgiref.validate-wrapped (compliance)."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from servery import wsgi
from servery.config import Config
from tests._harness import body_of, capturing_logs, raw_exchange, serving, status_of

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False

_SPEC = "tests._wsgiapp:application"


class LoadAppTest(unittest.TestCase):
    def test_loads_callable(self):
        self.assertTrue(callable(wsgi.load_app(_SPEC)))

    def test_bad_specs_raise(self):
        with self.assertRaises(ValueError):
            wsgi.load_app("tests._wsgiapp:does_not_exist")
        with self.assertRaises(ModuleNotFoundError):
            wsgi.load_app("no_such_module_xyz:app")


class ConfigTest(unittest.TestCase):
    def test_wsgi_and_http2_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            Config.create(".", wsgi_app=_SPEC, http2=True)


class WSGIServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "static.txt").write_text("file content")
        self.cfg = Config.create(
            self._tmp.name, host="127.0.0.1", port=0, quiet=True, wsgi_app=_SPEC
        )

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
    def test_methods_and_body_echo(self):
        with serving(self.cfg) as (host, port):
            base = f"http://{host}:{port}"
            with httpx.Client() as client:
                got = client.get(f"{base}/p?x=1")
                self.assertEqual(got.status_code, 200)
                self.assertEqual(got.text, "GET /p ")
                posted = client.post(f"{base}/up", content=b"DATA")
                self.assertEqual(posted.text, "POST /up DATA")
                for method in ("PUT", "DELETE", "PATCH"):
                    resp = client.request(method, f"{base}/m", content=b"x")
                    self.assertEqual(resp.status_code, 200, method)
                    self.assertTrue(resp.text.startswith(method), resp.text)

    def test_static_files_not_served_in_wsgi_mode(self):
        # The WSGI app owns every path — the static file is invisible.
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET /static.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(resp), 200)
            self.assertIn(b"GET /static.txt", body_of(resp))  # app echo, not the file

    def test_content_length_response_keeps_connection_alive(self):
        # Two pipelined requests on one connection -> keep-alive works.
        with serving(self.cfg) as (host, port):
            req = (
                b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n"
                b"GET /b HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            resp = raw_exchange(host, port, req)
            self.assertEqual(resp.count(b"200 OK"), 2)
            self.assertIn(b"GET /a", resp)
            self.assertIn(b"GET /b", resp)


class ChunkedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config.create(
            self._tmp.name,
            host="127.0.0.1",
            port=0,
            quiet=True,
            wsgi_app="tests._wsgiapp:streaming",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_content_length_uses_chunked(self):
        import socket

        # A keep-alive request (no Connection: close) with no Content-Length must
        # be framed with chunked transfer-encoding, not close-delimited.
        with serving(self.cfg) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                sock.settimeout(3)
                data = b""
                while b"0\r\n\r\n" not in data:  # read to the chunked terminator
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
            finally:
                sock.close()
            self.assertIn(b"transfer-encoding: chunked", data.split(b"\r\n\r\n", 1)[0].lower())
            for marker in (b"chunk1", b"chunk2", b"chunk3"):
                self.assertIn(marker, data)


class MaterializedTest(unittest.TestCase):
    """The fast path: a raw list body -> engine adds Content-Length, one write."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config.create(
            self._tmp.name,
            host="127.0.0.1",
            port=0,
            quiet=True,
            wsgi_app="tests._wsgiapp:plain_list",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_engine_adds_content_length(self):
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            head = resp.split(b"\r\n\r\n", 1)[0].lower()
            self.assertEqual(status_of(resp), 200)
            self.assertIn(b"content-length: 16", head)  # "materialized GET"
            self.assertNotIn(b"transfer-encoding", head)
            self.assertEqual(body_of(resp), b"materialized GET")

    def test_head_sends_headers_without_body(self):
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"HEAD / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(resp), 200)
            self.assertIn(
                b"content-length: 17", resp.split(b"\r\n\r\n", 1)[0].lower()
            )  # HEAD body len
            self.assertEqual(body_of(resp), b"")


class WSGITelemetryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config.create(
            self._tmp.name,
            host="127.0.0.1",
            port=0,
            quiet=True,
            wsgi_app="tests._wsgiapp:crashing",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_app_error_returns_500_and_is_logged(self):
        with capturing_logs(logging.ERROR) as cap, serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET /x HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
        self.assertEqual(status_of(resp), 500)
        self.assertTrue(any("WSGI app error" in m for m in cap.messages()), cap.messages())


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class WSGIAuthTest(unittest.TestCase):
    def test_auth_is_enforced(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.create(
            tmp.name, host="127.0.0.1", port=0, quiet=True, wsgi_app=_SPEC, auth="u:p"
        )
        with serving(cfg) as (host, port), httpx.Client() as client:
            unauth = client.get(f"http://{host}:{port}/x")
            self.assertEqual(unauth.status_code, 401)  # was silently bypassed
            ok = client.get(f"http://{host}:{port}/x", auth=("u", "p"))
            self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()

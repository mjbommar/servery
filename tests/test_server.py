"""End-to-end server tests: spin up on an ephemeral port and make real requests."""

import contextlib
import http.client
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path

from servery.config import Config
from servery.server import make_server, server_url


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "hello.txt").write_text("hi there")
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "nested.txt").write_text("deep")
        (self.dir / ".secret").write_text("nope")

        config = Config.create(self.dir, host="127.0.0.1", port=0, quiet=True)
        self.httpd = make_server(config)
        self.host = str(self.httpd.server_address[0])
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    def _conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=5)

    def test_server_url(self):
        url = server_url(self.httpd)
        self.assertEqual(url, f"http://{self.host}:{self.port}/")

    def test_serves_file(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"hi there")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_directory_listing(self):
        conn = self._conn()
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type", ""))
        self.assertIn("hello.txt", body)
        self.assertIn("sub/", body)
        self.assertNotIn(".secret", body)

    def test_404_for_missing(self):
        conn = self._conn()
        conn.request("GET", "/does-not-exist")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_http_1_1_and_keep_alive(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        resp1 = conn.getresponse()
        self.assertEqual(resp1.version, 11)  # HTTP/1.1
        resp1.read()
        # Reuse the same connection — only possible with persistent connections.
        conn.request("GET", "/sub/nested.txt")
        resp2 = conn.getresponse()
        body2 = resp2.read()
        conn.close()
        self.assertEqual(body2, b"deep")

    def test_directory_redirect_adds_slash(self):
        conn = self._conn()
        conn.request("GET", "/sub")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 301)
        self.assertTrue(resp.getheader("Location", "").endswith("/sub/"))

    def test_request_logging_when_not_quiet(self):
        config = Config.create(self.dir, host="127.0.0.1", port=0, quiet=False)
        httpd = make_server(config)
        host = str(httpd.server_address[0])
        port = int(httpd.server_address[1])
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/hello.txt")
                conn.getresponse().read()
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        self.assertIn("GET", buf.getvalue())

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_symlink_escape_blocked(self):
        outside = Path(self._tmp.name).parent / "servery_outside_target.txt"
        outside.write_text("LEAK")
        link = self.dir / "escape.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlink creation not permitted")
        try:
            conn = self._conn()
            conn.request("GET", "/escape.txt")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 404)
            self.assertNotIn(b"LEAK", body)
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

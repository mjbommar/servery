"""End-to-end server tests: spin up on an ephemeral port and make real requests."""

import contextlib
import email.utils
import http.client
import io
import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from servery.config import Config
from servery.server import make_server, server_url


def _multipart_body(boundary: str, filename: str, content: bytes) -> bytes:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'
    ).encode()
    return header + content + f"\r\n--{boundary}--\r\n".encode()


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

    def test_post_rejected_when_upload_disabled(self):
        conn = self._conn()
        conn.request("POST", "/", b"--B--\r\n", {"Content-Type": "multipart/form-data; boundary=B"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 404)

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

    def test_full_response_advertises_range_and_etag(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.getheader("Accept-Ranges"), "bytes")
        self.assertTrue(resp.getheader("ETag", "").startswith('"'))

    def test_range_partial(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt", headers={"Range": "bytes=0-3"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 206)
        self.assertEqual(body, b"hi t")
        self.assertEqual(resp.getheader("Content-Range"), "bytes 0-3/8")
        self.assertEqual(resp.getheader("Content-Length"), "4")

    def test_range_suffix(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt", headers={"Range": "bytes=-3"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 206)
        self.assertEqual(body, b"ere")
        self.assertEqual(resp.getheader("Content-Range"), "bytes 5-7/8")

    def test_range_unsatisfiable(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt", headers={"Range": "bytes=100-200"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 416)
        self.assertEqual(resp.getheader("Content-Range"), "bytes */8")

    def test_conditional_if_none_match(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        first = conn.getresponse()
        first.read()
        etag = first.getheader("ETag")
        assert etag is not None
        conn.request("GET", "/hello.txt", headers={"If-None-Match": etag})
        second = conn.getresponse()
        body = second.read()
        conn.close()
        self.assertEqual(second.status, 304)
        self.assertEqual(body, b"")

    def test_conditional_if_modified_since_future(self):
        future = email.utils.formatdate(time.time() + 3600, usegmt=True)
        conn = self._conn()
        conn.request("GET", "/hello.txt", headers={"If-Modified-Since": future})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 304)

    def test_if_range_match_honors_range(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        first = conn.getresponse()
        first.read()
        etag = first.getheader("ETag")
        assert etag is not None
        conn.request("GET", "/hello.txt", headers={"Range": "bytes=0-3", "If-Range": etag})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 206)
        self.assertEqual(body, b"hi t")

    def test_if_range_etag_mismatch_serves_full(self):
        conn = self._conn()
        conn.request(
            "GET",
            "/hello.txt",
            headers={"Range": "bytes=0-3", "If-Range": '"deadbeef-1"'},
        )
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"hi there")

    def test_if_range_stale_date_serves_full(self):
        past = email.utils.formatdate(time.time() - 3600, usegmt=True)
        conn = self._conn()
        conn.request("GET", "/hello.txt", headers={"Range": "bytes=0-3", "If-Range": past})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)

    def test_listing_accepts_sort_and_query(self):
        conn = self._conn()
        conn.request("GET", "/?C=S&O=D&q=hello")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("hello.txt", body)

    def test_index_html_is_served(self):
        site = self.dir / "site"
        site.mkdir()
        (site / "index.html").write_text("<h1>home</h1>")
        conn = self._conn()
        conn.request("GET", "/site/")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"home", body)
        # Served via the file path now, so it gets an ETag.
        self.assertTrue(resp.getheader("ETag"))

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_index_symlink_escape_blocked(self):
        outside = Path(self._tmp.name).parent / "servery_outside_index.html"
        outside.write_text("TOPSECRET")
        site = self.dir / "docs"
        site.mkdir()
        link = site / "index.html"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            self.skipTest("symlink creation not permitted")
        try:
            conn = self._conn()
            conn.request("GET", "/docs/")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertNotIn(b"TOPSECRET", body)
            self.assertIn(b"Index of", body)  # fell back to a listing, did not leak
        finally:
            outside.unlink(missing_ok=True)

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


class AuthServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        (directory / "hello.txt").write_text("private")
        config = Config.create(directory, host="127.0.0.1", port=0, quiet=True, auth="alice:secret")
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

    def _request(self, headers: dict[str, str]) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("GET", "/hello.txt", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    @staticmethod
    def _basic(username: str, password: str) -> str:
        import base64

        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")

    def test_401_without_credentials(self):
        resp = self._request({})
        self.assertEqual(resp.status, 401)
        self.assertIn("Basic", resp.getheader("WWW-Authenticate", ""))

    def test_200_with_valid_credentials(self):
        resp = self._request({"Authorization": self._basic("alice", "secret")})
        self.assertEqual(resp.status, 200)

    def test_401_with_wrong_credentials(self):
        resp = self._request({"Authorization": self._basic("alice", "nope")})
        self.assertEqual(resp.status, 401)


class UploadServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        config = Config.create(
            self.dir, host="127.0.0.1", port=0, quiet=True, upload=True, max_upload_size=1024
        )
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

    def _post(self, body: bytes, content_type: str = "multipart/form-data; boundary=B"):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("POST", "/", body, {"Content-Type": content_type})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    def test_upload_creates_file(self):
        resp = self._post(_multipart_body("B", "up.txt", b"payload"))
        self.assertEqual(resp.status, 303)
        self.assertEqual((self.dir / "up.txt").read_bytes(), b"payload")

    def test_listing_shows_upload_form(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertIn('type="file"', body)

    def test_upload_too_large_returns_413(self):
        resp = self._post(_multipart_body("B", "big.txt", b"x" * 2000))
        self.assertEqual(resp.status, 413)

    def test_wrong_content_type_returns_415(self):
        resp = self._post(b"plain body", content_type="text/plain")
        self.assertEqual(resp.status, 415)


class TlsServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        (directory / "hello.txt").write_text("secure hi")
        cert = directory / "cert.pem"
        key = directory / "key.pem"
        try:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            self._tmp.cleanup()
            self.skipTest("openssl not available")

        config = Config.create(
            directory,
            host="127.0.0.1",
            port=0,
            quiet=True,
            tls_cert=str(cert),
            tls_key=str(key),
        )
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

    def _client_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def test_https_serves_file_with_hsts(self):
        conn = http.client.HTTPSConnection(
            self.host, self.port, timeout=5, context=self._client_context()
        )
        conn.request("GET", "/hello.txt")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"secure hi")
        self.assertIn("max-age", resp.getheader("Strict-Transport-Security", ""))

    def test_server_url_is_https(self):
        self.assertTrue(server_url(self.httpd).startswith("https://"))


if __name__ == "__main__":
    unittest.main()

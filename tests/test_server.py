"""End-to-end server tests: spin up on an ephemeral port and make real requests."""

import contextlib
import email.utils
import http.client
import io
import logging
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from servery import _listener
from servery.config import Config
from servery.handler import ServeryHandler
from servery.server import make_server, server_url
from tests._harness import capturing_logs, raw_exchange


def _multipart_body(boundary: str, filename: str, content: bytes) -> bytes:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n\r\n'
    ).encode()
    return header + content + f"\r\n--{boundary}--\r\n".encode()


@contextlib.contextmanager
def _running(config: Config):
    httpd = make_server(config)
    host = str(httpd.server_address[0])
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


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
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        # The on-brand styled error page (not the bland stdlib default).
        self.assertIn("<title>404", body)
        self.assertIn("served by servery", body)
        self.assertIn('href="/"', body)  # a link back home
        self.assertNotIn("Error response", body)  # the stdlib default title is gone

    def test_text_file_declares_utf8_charset(self):
        # Text types must declare UTF-8 so browsers don't mojibake non-ASCII content.
        (self.dir / "note.md").write_text("em — dash, “curly”, café 🙂", encoding="utf-8")
        conn = self._conn()
        conn.request("GET", "/note.md")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.getheader("Content-Type"), "text/markdown; charset=utf-8")

    def test_listing_has_csp_and_referrer(self):
        conn = self._conn()
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertIn("default-src", resp.getheader("Content-Security-Policy", ""))
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")

    def test_file_has_cache_control_but_no_csp(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        self.assertIsNone(resp.getheader("Content-Security-Policy"))

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

    def test_download_query_forces_attachment(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt?download=1")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"hi there")
        self.assertIn("attachment", resp.getheader("Content-Disposition", ""))
        self.assertIn("hello.txt", resp.getheader("Content-Disposition", ""))

    def test_no_download_header_without_query(self):
        conn = self._conn()
        conn.request("GET", "/hello.txt")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertIsNone(resp.getheader("Content-Disposition"))

    def test_theme_param_sets_cookie_and_attribute(self):
        conn = self._conn()
        conn.request("GET", "/?theme=dark")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        cookie = resp.getheader("Set-Cookie", "")
        conn.close()
        self.assertIn("servery_theme=dark", cookie)
        self.assertIn('data-theme="dark"', body)

    def test_theme_cookie_is_honored(self):
        conn = self._conn()
        conn.request("GET", "/", headers={"Cookie": "servery_theme=light"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertIn('data-theme="light"', body)
        # No explicit param this time, so nothing is re-set.
        self.assertIsNone(resp.getheader("Set-Cookie"))

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

    def test_archive_targz(self):
        conn = self._conn()
        conn.request("GET", "/?archive=tar.gz")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.getheader("Content-Disposition", ""))
        self.assertEqual(resp.getheader("Transfer-Encoding"), "chunked")
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertTrue(any(name.endswith("hello.txt") for name in names))
        self.assertTrue(any(name.endswith("sub/nested.txt") for name in names))

    def test_archive_zip(self):
        import zipfile

        conn = self._conn()
        conn.request("GET", "/?archive=zip")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertTrue(any(name.endswith("hello.txt") for name in zf.namelist()))

    def test_request_logging_via_logging_module(self):
        import logging

        from servery import _log

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages: list[str] = []

            def emit(self, record):
                self.messages.append(record.getMessage())

        handler = _Capture()
        _log.logger.addHandler(handler)
        previous = _log.logger.level
        _log.logger.setLevel(logging.INFO)
        try:
            conn = self._conn()
            conn.request("GET", "/hello.txt")
            conn.getresponse().read()
            conn.close()
        finally:
            _log.logger.removeHandler(handler)
            _log.logger.setLevel(previous)
        self.assertTrue(any("GET" in message for message in handler.messages))

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


class FeatureFlagTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "index.html").write_text("<h1>app</h1>")
        (self.dir / "f.txt").write_text("data")

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, **kwargs) -> Config:
        return Config.create(self.dir, host="127.0.0.1", port=0, quiet=True, **kwargs)

    @staticmethod
    def _get(host, port, path, method="GET"):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_cors_header(self):
        with _running(self._config(cors=True)) as (host, port):
            resp, _ = self._get(host, port, "/f.txt")
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")

    def test_options_preflight(self):
        with _running(self._config(cors=True)) as (host, port):
            resp, _ = self._get(host, port, "/", method="OPTIONS")
            self.assertEqual(resp.status, 204)
            self.assertIn("GET", resp.getheader("Access-Control-Allow-Methods", ""))
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")

    def test_spa_fallback(self):
        with _running(self._config(spa=True)) as (host, port):
            resp, body = self._get(host, port, "/client/side/route")
            self.assertEqual(resp.status, 200)
            self.assertIn(b"app", body)

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_spa_fallback_rejects_index_symlink_escape(self):
        outside = self.dir.parent / f"{self.dir.name}-outside-spa.html"
        outside.write_text("TOP SECRET")
        (self.dir / "index.html").unlink()
        try:
            try:
                (self.dir / "index.html").symlink_to(outside)
            except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
                self.skipTest("symlink creation not permitted")
            with _running(self._config(spa=True)) as (host, port):
                resp, body = self._get(host, port, "/client/side/route")
            self.assertEqual(resp.status, 404)
            self.assertNotIn(b"TOP SECRET", body)
        finally:
            outside.unlink(missing_ok=True)

    def test_cache_max_age(self):
        with _running(self._config(cache_max_age=3600)) as (host, port):
            resp, _ = self._get(host, port, "/f.txt")
            self.assertEqual(resp.getheader("Cache-Control"), "max-age=3600")

    def test_security_headers_can_be_disabled(self):
        with _running(self._config(security_headers=False)) as (host, port):
            resp, _ = self._get(host, port, "/")
            self.assertIsNone(resp.getheader("X-Content-Type-Options"))
            self.assertIsNone(resp.getheader("Content-Security-Policy"))

    def test_options_after_listing_has_no_csp(self):
        # The _generated_page flag must not leak from a listing into a later
        # bodiless response on the same keep-alive connection.
        with _running(self._config(cors=True)) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/")
            conn.getresponse().read()
            conn.request("OPTIONS", "/")
            resp = conn.getresponse()
            resp.read()
            conn.close()
        self.assertEqual(resp.status, 204)
        self.assertIsNone(resp.getheader("Content-Security-Policy"))

    def test_bounded_concurrency_serves(self):
        with _running(self._config(max_workers=2)) as (host, port):
            for _ in range(3):
                resp, body = self._get(host, port, "/f.txt")
                self.assertEqual(resp.status, 200)
                self.assertEqual(body, b"data")

    def test_explicit_write_timeout_preserves_static_response(self):
        with _running(self._config(write_timeout=1.0)) as (host, port):
            resp, body = self._get(host, port, "/f.txt")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"data")


class ConnectionBudgetTest(unittest.TestCase):
    def test_saturation_rejects_without_queueing_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "f.txt").write_text("ok")
            cfg = Config.create(
                tmp,
                host="127.0.0.1",
                port=0,
                quiet=True,
                max_connections=1,
                timeout=2,
            )
            with _running(cfg) as (host, port):
                held = socket.create_connection((host, port), timeout=5)
                held.sendall(b"GET /f.txt HTTP/1.1\r\n")
                time.sleep(0.1)
                rejected = socket.create_connection((host, port), timeout=5)
                try:
                    rejected.sendall(b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    rejected.settimeout(2)
                    try:
                        data = rejected.recv(4096)
                    except ConnectionResetError:
                        data = b""
                    self.assertEqual(data, b"")
                finally:
                    rejected.close()
                    held.close()

                deadline = time.monotonic() + 3
                status = None
                while time.monotonic() < deadline and status != 200:
                    conn = http.client.HTTPConnection(host, port, timeout=2)
                    try:
                        conn.request("GET", "/f.txt", headers={"Connection": "close"})
                        response = conn.getresponse()
                        response.read()
                        status = response.status
                    except OSError:
                        time.sleep(0.05)
                    finally:
                        conn.close()
                self.assertEqual(status, 200)


class GracefulDrainTest(unittest.TestCase):
    def _server(self, directory: str, **overrides):
        return make_server(
            Config.create(
                directory,
                host="127.0.0.1",
                port=0,
                quiet=True,
                **overrides,
            )
        )

    def test_worker_queue_rejection_removes_registry_and_releases_once(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._server(
                directory,
                max_workers=1,
                max_connections=1,
                drain_timeout=0,
            )
            left, right = socket.socketpair()
            try:
                assert server._slots is not None
                for _ in range(4):
                    self.assertTrue(server._slots.acquire(blocking=False))

                server.process_request(left, ("local", 1))

                self.assertEqual(server._active_sockets, set())
                self.assertEqual(server._connection_permits, set())
                assert server._connections is not None
                self.assertTrue(server._connections.acquire(blocking=False))
                self.assertFalse(server._connections.acquire(blocking=False))
                # A duplicate cleanup must not over-release a bounded semaphore.
                server._finish_connection(left)
                self.assertFalse(server._connections.acquire(blocking=False))
                server._connections.release()
            finally:
                right.close()
                for _ in range(4):
                    server._slots.release()
                server.server_close()

    def test_worker_submit_failure_releases_slot_and_connection_once(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._server(
                directory,
                max_workers=1,
                max_connections=1,
                drain_timeout=0,
            )
            left, right = socket.socketpair()
            try:
                assert server._executor is not None
                with (
                    mock.patch.object(
                        server._executor,
                        "submit",
                        side_effect=RuntimeError("executor stopped"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "executor stopped"),
                ):
                    server.process_request(left, ("local", 1))

                assert server._slots is not None
                permits = 0
                while server._slots.acquire(blocking=False):
                    permits += 1
                self.assertEqual(permits, 4)
                for _ in range(permits):
                    server._slots.release()
                self.assertEqual(server._active_sockets, set())
                self.assertEqual(server._connection_permits, set())
                assert server._connections is not None
                self.assertTrue(server._connections.acquire(blocking=False))
                self.assertFalse(server._connections.acquire(blocking=False))
                server._connections.release()
            finally:
                right.close()
                server.server_close()

    def test_protocol_notification_never_blocks_begin_draining(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._server(directory, drain_timeout=0)
            request = object()
            entered = threading.Event()
            release = threading.Event()

            def blocking_notification():
                entered.set()
                release.wait(2)

            with server._drain_condition:
                server._active_sockets.add(request)
            server.register_connection_drainer(request, blocking_notification)
            started = time.monotonic()
            server.begin_draining()
            elapsed = time.monotonic() - started
            try:
                self.assertLess(elapsed, 0.2)
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
                server._finish_connection(request)
                server.server_close()

    def test_deadline_force_closes_unfinished_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._server(directory, drain_timeout=0.05)
            active, peer = socket.socketpair()
            try:
                with server._drain_condition:
                    server._active_sockets.add(active)
                started = time.monotonic()
                with self.assertLogs("servery", level="WARNING") as logs:
                    server.server_close()
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.03)
                self.assertLess(elapsed, 0.5)
                peer.settimeout(1)
                self.assertEqual(peer.recv(1), b"")
                self.assertIn("force-closing 1 HTTP/1/application", "\n".join(logs.output))
            finally:
                server._finish_connection(active)
                peer.close()

    def test_server_close_does_not_wait_for_executor_threads(self):
        class ExecutorProbe:
            def __init__(self):
                self.calls = []

            def shutdown(self, *, wait, cancel_futures):
                self.calls.append((wait, cancel_futures))

        with tempfile.TemporaryDirectory() as directory:
            server = self._server(directory, drain_timeout=0)
            probe = ExecutorProbe()
            server._executor = probe
            server.server_close()
            self.assertEqual(probe.calls, [(False, True)])

    def test_inflight_http1_response_advertises_close_during_drain(self):
        started = threading.Event()
        continue_response = threading.Event()
        body = b"ok" * (64 * 1024)

        class BlockingHandler(ServeryHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler dispatch contract
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                started.set()
                if not continue_response.wait(2):
                    raise AssertionError("test did not release response")
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as directory:
            server = self._server(directory, drain_timeout=1)
            server._handler_cls = BlockingHandler
            host, port = server.server_address[:2]
            serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
            serve_thread.start()
            sock = socket.create_connection((host, port), timeout=2)
            shutdown_thread = threading.Thread(target=server.shutdown, daemon=True)
            try:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                self.assertTrue(started.wait(1))
                shutdown_thread.start()
                deadline = time.monotonic() + 1
                while not server.is_draining and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(server.is_draining)
                continue_response.set()
                response = bytearray()
                while chunk := sock.recv(4096):
                    response.extend(chunk)
                shutdown_thread.join(2)
                self.assertFalse(shutdown_thread.is_alive())
                self.assertIn(b"Connection: close\r\n", response)
                self.assertTrue(response.endswith(body))
            finally:
                continue_response.set()
                sock.close()
                if shutdown_thread.is_alive():
                    shutdown_thread.join(2)
                server.server_close()
                serve_thread.join(2)

    def test_admitted_upload_completes_during_drain(self):
        started = threading.Event()
        continue_upload = threading.Event()

        class BlockingUploadHandler(ServeryHandler):
            def do_POST(self):  # noqa: N802 - stdlib handler dispatch contract
                started.set()
                if not continue_upload.wait(2):
                    raise AssertionError("test did not release upload")
                super().do_POST()

        with tempfile.TemporaryDirectory() as directory:
            server = self._server(directory, upload=True, drain_timeout=1)
            server._handler_cls = BlockingUploadHandler
            host, port = server.server_address[:2]
            serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
            serve_thread.start()
            body = _multipart_body("DRAIN", "accepted.txt", b"accepted-upload")
            request = (
                b"POST / HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: multipart/form-data; boundary=DRAIN\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            sock = socket.create_connection((host, port), timeout=2)
            shutdown_thread = threading.Thread(target=server.shutdown, daemon=True)
            try:
                sock.sendall(request)
                self.assertTrue(started.wait(1))
                shutdown_thread.start()
                deadline = time.monotonic() + 1
                while not server.is_draining and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(server.is_draining)
                continue_upload.set()
                response = bytearray()
                while chunk := sock.recv(4096):
                    response.extend(chunk)
                shutdown_thread.join(2)
                self.assertFalse(shutdown_thread.is_alive())
                self.assertIn(b" 303 ", response)
                self.assertEqual(Path(directory, "accepted.txt").read_bytes(), b"accepted-upload")
            finally:
                continue_upload.set()
                sock.close()
                if shutdown_thread.is_alive():
                    shutdown_thread.join(2)
                server.server_close()
                serve_thread.join(2)

    def test_begin_draining_and_close_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self._server(directory, drain_timeout=0)
            server.begin_draining()
            deadline = server._drain_deadline
            server.begin_draining()
            self.assertEqual(server._drain_deadline, deadline)
            server.server_close()
            server.server_close()


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


class HandleErrorTest(unittest.TestCase):
    """ServeryHTTPServer.handle_error routes through our logger by severity."""

    def _server(self):
        return make_server(Config.create(".", host="127.0.0.1", port=0, quiet=True))

    def test_client_transport_error_is_debug_only(self):
        srv = self._server()
        try:
            with capturing_logs(logging.DEBUG) as cap:
                try:
                    raise ConnectionResetError("peer reset")  # noqa: TRY301 (set exc_info)
                except ConnectionResetError:
                    srv.handle_error(None, ("1.2.3.4", 5))
            self.assertFalse(any(r.levelno >= logging.WARNING for r in cap.records), cap.messages())
            self.assertTrue(any("transport error" in m for m in cap.messages()), cap.messages())
        finally:
            srv.server_close()

    def test_unexpected_error_logged_at_error(self):
        srv = self._server()
        try:
            with capturing_logs(logging.ERROR) as cap:
                try:
                    raise ValueError("unexpected bug")  # noqa: TRY301 (set exc_info)
                except ValueError:
                    srv.handle_error(None, ("1.2.3.4", 5))
            self.assertTrue(any(r.levelno == logging.ERROR for r in cap.records))
            self.assertTrue(any("unhandled error" in m for m in cap.messages()), cap.messages())
        finally:
            srv.server_close()


class RequestLimitTest(unittest.TestCase):
    def test_final_response_advertises_close_and_pipeline_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "f.txt").write_text("limited-body")
            config = Config.create(
                directory,
                host="127.0.0.1",
                port=0,
                quiet=True,
                max_requests_per_connection=1,
            )
            request = (
                b"GET /f.txt HTTP/1.1\r\nHost: x\r\n\r\n"
                b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            with _running(config) as (host, port):
                sock = socket.create_connection((host, port), timeout=5)
                try:
                    sock.sendall(request)
                    response = bytearray()
                    while chunk := sock.recv(65536):
                        response.extend(chunk)
                finally:
                    sock.close()
        self.assertIn(b"Connection: close", response)
        self.assertEqual(response.count(b"limited-body"), 1)

    def test_zero_keeps_unlimited_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "f.txt").write_text("unlimited-body")
            config = Config.create(
                directory,
                host="127.0.0.1",
                port=0,
                quiet=True,
                max_requests_per_connection=0,
            )
            request = (
                b"GET /f.txt HTTP/1.1\r\nHost: x\r\n\r\n"
                b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            with _running(config) as (host, port):
                sock = socket.create_connection((host, port), timeout=5)
                try:
                    sock.sendall(request)
                    response = bytearray()
                    while chunk := sock.recv(65536):
                        response.extend(chunk)
                finally:
                    sock.close()
        self.assertEqual(response.count(b"unlimited-body"), 2)


class PortAutoScanTest(unittest.TestCase):
    @unittest.skipUnless(
        os.name == "posix",
        "Windows SO_REUSEADDR lets a second bind share the port, so it never raises "
        "EADDRINUSE for the scan to react to",
    )
    def test_busy_port_scans_to_next_free(self):
        import socket

        # Occupy a port, then ask servery for it — it should bind the next one.
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        try:
            cfg = Config.create(".", host="127.0.0.1", port=taken, quiet=True)
            httpd = make_server(cfg)
            try:
                self.assertNotEqual(httpd.server_address[1], taken)
                self.assertGreater(httpd.server_address[1], taken)
            finally:
                httpd.server_close()
        finally:
            busy.close()

    def test_ephemeral_port_binds_directly(self):
        cfg = Config.create(".", host="127.0.0.1", port=0, quiet=True)
        httpd = make_server(cfg)
        try:
            self.assertGreater(httpd.server_address[1], 0)
        finally:
            httpd.server_close()


class ListenerAdoptionTest(unittest.TestCase):
    @staticmethod
    def _config() -> Config:
        return Config.create(".", host="127.0.0.1", port=0, quiet=True)

    def test_runtime_close_does_not_close_callers_listener(self):
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        try:
            httpd = make_server(self._config(), listener=listener)
            self.assertEqual(httpd.server_address, listener.getsockname())
            httpd.server_close()
            self.assertGreaterEqual(listener.fileno(), 0)
            probe = socket.create_connection(listener.getsockname(), timeout=1)
            probe.close()
        finally:
            listener.close()

    def test_callers_close_does_not_stop_adopted_runtime(self):
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        address = listener.getsockname()
        httpd = make_server(self._config(), listener=listener)
        listener.close()
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            response = raw_exchange(
                str(address[0]),
                int(address[1]),
                b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
            self.assertIn(b"200 OK", response)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_rejects_bound_socket_that_is_not_listening(self):
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            try:
                listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            except OSError:
                self.skipTest("platform cannot query SO_ACCEPTCONN")
            with self.assertRaisesRegex(ValueError, "already be listening"):
                make_server(self._config(), listener=listener)
            self.assertGreaterEqual(listener.fileno(), 0)
        finally:
            listener.close()

    def test_two_runtime_generations_adopt_and_close_independently(self):
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        address = listener.getsockname()
        with (
            tempfile.TemporaryDirectory() as first_dir,
            tempfile.TemporaryDirectory() as second_dir,
        ):
            Path(first_dir, "generation.txt").write_text("first")
            Path(second_dir, "generation.txt").write_text("second")
            first = make_server(
                Config.create(first_dir, host="127.0.0.1", port=0, quiet=True),
                listener=listener,
            )
            second = make_server(
                Config.create(second_dir, host="127.0.0.1", port=0, quiet=True),
                listener=listener,
            )

            def serve_generation(server):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    response = raw_exchange(
                        str(address[0]),
                        int(address[1]),
                        b"GET /generation.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                return response

            try:
                self.assertTrue(serve_generation(first).endswith(b"first"))
                self.assertTrue(serve_generation(second).endswith(b"second"))
                probe = socket.create_connection(listener.getsockname(), timeout=1)
                probe.close()
            finally:
                first.server_close()
                second.server_close()
                listener.close()

    def test_tls_wraps_only_the_runtime_duplicate(self):
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        try:
            config = Config.create(
                ".",
                host="127.0.0.1",
                port=0,
                quiet=True,
                tls_self_signed=True,
            )
            httpd = make_server(config, listener=listener)
            try:
                self.assertIsInstance(httpd.socket, ssl.SSLSocket)
                self.assertNotIsInstance(listener, ssl.SSLSocket)
                probe = socket.create_connection(listener.getsockname(), timeout=1)
                probe.close()
            finally:
                httpd.server_close()
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()

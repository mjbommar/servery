"""Security regression + abuse-case tests.

End-to-end guards for bugs the review loops fixed, plus resource-abuse vectors
(oversized request line/headers, idle-client timeout, header injection).
"""

from __future__ import annotations

import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from servery.config import Config
from tests._harness import raw_exchange, serving, status_of


class AbuseLimitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("ok")
        self.cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_request_uri_too_long(self):
        with serving(self.cfg) as (host, port):
            request = b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            self.assertEqual(status_of(raw_exchange(host, port, request)), 414)

    def test_too_many_headers(self):
        with serving(self.cfg) as (host, port):
            bloat = "".join(f"X-H{i}: v\r\n" for i in range(200))
            request = f"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{bloat}\r\n".encode()
            self.assertEqual(status_of(raw_exchange(host, port, request)), 431)

    def test_idle_client_times_out(self):
        # A client that opens a request but never finishes it must be dropped, not
        # held forever (Slowloris). Use a short per-connection timeout.
        cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True, timeout=0.5)
        with serving(cfg) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET /f.txt HTTP/1.1\r\n")  # no blank line: request never completes
                sock.settimeout(4)
                # Server times out (~0.5s) and closes -> recv returns b"".
                self.assertEqual(sock.recv(64), b"")
            finally:
                sock.close()


class HeaderInjectionTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(__import__("os"), "mkdir"), "needs mkdir")
    def test_crlf_directory_name_does_not_inject_header(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        dirname = "ev\r\nX-Injected: pwned"
        try:
            (root / dirname).mkdir()
            (root / dirname / "inner.txt").write_text("x")
        except (OSError, ValueError):  # filesystem rejects CR/LF names
            tmp.cleanup()
            self.skipTest("filesystem does not allow CR/LF in names")
        cfg = Config.create(root, host="127.0.0.1", port=0, quiet=True)
        try:
            with serving(cfg) as (host, port):
                target = "/" + urllib.parse.quote(dirname) + "/?archive=tar.gz"
                request = f"GET {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
                resp = raw_exchange(host, port, request)
                # The CRLF must be stripped: "X-Injected" may appear inside the
                # (single-line) filename value, but never as its own header line.
                self.assertNotIn(b"\r\nX-Injected:", resp)
                self.assertEqual(status_of(resp), 200)
        finally:
            tmp.cleanup()


class UploadContainmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("ok")
        self.cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True, upload=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_post_to_a_file_path_is_404(self):
        # Upload destination must be a directory inside the root.
        body = (
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="x.txt"\r\n\r\n'
            b"DATA\r\n--B--\r\n"
        )
        request = (
            b"POST /f.txt HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: multipart/form-data; boundary=B\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        with serving(self.cfg) as (host, port):
            self.assertEqual(status_of(raw_exchange(host, port, request)), 404)


class AuthBypassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("secret")
        self.cfg = Config.create(
            self._tmp.name, host="127.0.0.1", port=0, quiet=True, auth="u:p", cors=True
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_requires_auth_options_preflight_does_not(self):
        with serving(self.cfg) as (host, port):
            get = b"GET /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            self.assertEqual(status_of(raw_exchange(host, port, get)), 401)
            # CORS preflight must succeed without credentials, by design.
            options = b"OPTIONS /f.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            self.assertEqual(status_of(raw_exchange(host, port, options)), 204)


if __name__ == "__main__":
    unittest.main()

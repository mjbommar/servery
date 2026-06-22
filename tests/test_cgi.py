"""CGI (--cgi) tests: RFC 3875 behavior + the security mitigations."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from servery import cgi
from servery.config import Config
from tests._harness import body_of, capturing_logs, raw_exchange, serving, status_of

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False

# A CGI script that reports request meta-vars + whether the dangerous ones leaked.
_ECHO = """import os, sys
data = sys.stdin.read()
print("Content-Type: text/plain")
print()
print("method=" + os.environ["REQUEST_METHOD"])
print("query=" + os.environ.get("QUERY_STRING", ""))
print("path_info=" + os.environ.get("PATH_INFO", ""))
print("proxy=" + str("HTTP_PROXY" in os.environ))
print("auth=" + str("HTTP_AUTHORIZATION" in os.environ))
print("xcustom=" + os.environ.get("HTTP_X_CUSTOM", ""))
print("body=" + data)
"""

_STATUS = """import sys
print("Status: 201 Created")
print("Content-Type: text/plain")
print()
print("created")
"""

_SLOW = """import time
time.sleep(5)
print("Content-Type: text/plain")
print()
"""

_CRASH = """import sys
sys.stderr.write("kaboom detail")
sys.exit(1)
"""


class ResolveScriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "app.py").write_text("x")
        self.root_real = str(self.root.resolve())

    def tearDown(self):
        self._tmp.cleanup()

    def test_splits_script_and_path_info(self):
        result = cgi.resolve_script(self.root_real, "/app.py/extra/bits")
        assert result is not None
        script, path_info = result
        self.assertTrue(script.endswith("app.py"))
        self.assertEqual(path_info, "/extra/bits")

    def test_missing_returns_none(self):
        self.assertIsNone(cgi.resolve_script(self.root_real, "/nope.py"))

    def test_traversal_cannot_escape(self):
        # A script in the PARENT of the cgi root must be unreachable via "..".
        outside = self.root.parent / "outside_cgi.py"
        outside.write_text("x")
        try:
            self.assertIsNone(cgi.resolve_script(self.root_real, "/../outside_cgi.py"))
        finally:
            outside.unlink()


class CGIServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "echo.py").write_text(_ECHO)
        (root / "status.py").write_text(_STATUS)
        self.cfg = Config.create(".", host="127.0.0.1", port=0, quiet=True, cgi_dir=str(root))

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
    def test_runs_script_with_request_data(self):
        with serving(self.cfg) as (host, port):
            with httpx.Client() as client:
                resp = client.post(
                    f"http://{host}:{port}/echo.py/extra?q=1",
                    content=b"PAYLOAD",
                    headers={"X-Custom": "ok"},
                )
            self.assertEqual(resp.status_code, 200)
            text = resp.text
            self.assertIn("method=POST", text)
            self.assertIn("query=q=1", text)
            self.assertIn("path_info=/extra", text)
            self.assertIn("xcustom=ok", text)
            self.assertIn("body=PAYLOAD", text)

    @unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
    def test_httpoxy_and_authorization_are_not_forwarded(self):
        with serving(self.cfg) as (host, port):
            with httpx.Client() as client:
                resp = client.get(
                    f"http://{host}:{port}/echo.py",
                    headers={"Proxy": "evil:8080", "Authorization": "Basic c2VjcmV0"},
                )
            self.assertIn("proxy=False", resp.text)  # httpoxy: HTTP_PROXY never set
            self.assertIn("auth=False", resp.text)  # Authorization never forwarded

    def test_status_header_is_honored(self):
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET /status.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(resp), 201)
            self.assertIn(b"created", body_of(resp))

    def test_missing_script_is_404(self):
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET /nope.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(resp), 404)


class CGITimeoutTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "slow.py").write_text(_SLOW)
        self.cfg = Config.create(".", host="127.0.0.1", port=0, quiet=True, cgi_dir=self._tmp.name)
        self._orig_timeout: float = cgi._TIMEOUT
        cgi._TIMEOUT = 0.5

    def tearDown(self):
        cgi._TIMEOUT = self._orig_timeout
        self._tmp.cleanup()

    def test_runaway_script_times_out(self):
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET /slow.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(resp), 504)


class CGIConfigTest(unittest.TestCase):
    def test_cgi_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            Config.create(".", cgi_dir="x", wsgi_app="m:a")
        with self.assertRaises(ValueError):
            Config.create(".", cgi_dir="x", http2=True)


class CGITelemetryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "crash.py").write_text(_CRASH)
        self.cfg = Config.create(".", host="127.0.0.1", port=0, quiet=True, cgi_dir=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_script_logs_stderr(self):
        with capturing_logs(logging.WARNING) as cap, serving(self.cfg) as (host, port):
            resp = raw_exchange(
                host, port, b"GET /crash.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
        self.assertEqual(status_of(resp), 502)
        # the script's own stderr is surfaced in the warning (key for debugging CGI)
        self.assertTrue(any("kaboom detail" in m for m in cap.messages()), cap.messages())


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class CGIAuthTest(unittest.TestCase):
    def test_auth_is_enforced(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "echo.py").write_text(_ECHO)
        cfg = Config.create(".", host="127.0.0.1", port=0, quiet=True, cgi_dir=tmp.name, auth="u:p")
        with serving(cfg) as (host, port), httpx.Client() as client:
            self.assertEqual(client.get(f"http://{host}:{port}/echo.py").status_code, 401)
            # OPTIONS must also require auth on a dynamic handler (not the unauth
            # file-server CORS preflight).
            self.assertEqual(client.options(f"http://{host}:{port}/echo.py").status_code, 401)
            ok = client.get(f"http://{host}:{port}/echo.py", auth=("u", "p"))
            self.assertEqual(ok.status_code, 200)

    def test_401_closes_connection_to_avoid_body_desync(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "echo.py").write_text(_ECHO)
        cfg = Config.create(".", host="127.0.0.1", port=0, quiet=True, cgi_dir=tmp.name, auth="u:p")
        with serving(cfg) as (host, port):
            # A rejected POST with a body that smuggles a second request line: the
            # connection must close so the body isn't parsed as the next request.
            resp = raw_exchange(
                host,
                port,
                b"POST /echo.py HTTP/1.1\r\nHost: x\r\nContent-Length: 30\r\n\r\n"
                b"GET /echo.py HTTP/1.1\r\nHost: x\r\n",
            )
            self.assertEqual(status_of(resp), 401)
            self.assertIn(b"connection: close", resp.lower())
            self.assertEqual(resp.count(b"HTTP/1.1 "), 1)  # the smuggled GET was NOT served


if __name__ == "__main__":
    unittest.main()

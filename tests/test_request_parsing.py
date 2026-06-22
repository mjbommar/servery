"""Tests for the fast request-header parser that replaces email.feedparser."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from servery.config import Config
from servery.handler import _read_request_headers, _RequestHeaders
from tests._harness import raw_exchange, serving, status_of


class RequestHeadersTest(unittest.TestCase):
    def test_case_insensitive_first_wins(self):
        h = _RequestHeaders([("Content-Type", "text/html"), ("content-type", "later")])
        self.assertEqual(h.get("CONTENT-type"), "text/html")  # first occurrence wins

    def test_get_default_and_missing(self):
        h = _RequestHeaders([("X-A", "1")])
        self.assertIsNone(h.get("missing"))
        self.assertEqual(h.get("missing", "d"), "d")
        self.assertEqual(h.get("x-a"), "1")

    def test_contains_and_getitem(self):
        h = _RequestHeaders([("X-A", "1")])
        self.assertIn("x-a", h)
        self.assertNotIn("x-b", h)
        self.assertNotIn(123, h)
        self.assertEqual(h["X-A"], "1")
        self.assertIsNone(h["nope"])  # email.Message semantics: None, not KeyError
        self.assertEqual(h.items(), [("X-A", "1")])


class ReadHeadersTest(unittest.TestCase):
    def _read(self, data: bytes) -> _RequestHeaders:
        return _read_request_headers(io.BytesIO(data))

    def test_basic_and_ows_trim(self):
        h = self._read(b"Host: example\r\nX-Pad:   spaced\t \r\n\r\n")
        self.assertEqual(h.get("Host"), "example")
        self.assertEqual(h.get("X-Pad"), "spaced")

    def test_obs_fold_merged(self):
        h = self._read(b"X-Long: a\r\n  b\r\n\tc\r\n\r\n")
        self.assertEqual(h.get("X-Long"), "a b c")

    def test_line_without_colon_ignored(self):
        h = self._read(b"Host: x\r\nGARBAGE-NO-COLON\r\nX-Ok: 1\r\n\r\n")
        self.assertEqual(h.get("Host"), "x")
        self.assertEqual(h.get("X-Ok"), "1")

    def test_terminated_by_eof(self):
        h = self._read(b"Host: x\r\n")  # no blank line, stream ends
        self.assertEqual(h.get("Host"), "x")


class RequestLineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("hi there")
        self.cfg = Config.create(self._tmp.name, host="127.0.0.1", port=0, quiet=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_case_insensitive_header_used(self):
        # A lowercase "range:" must still trigger a 206 (header names are folded).
        with serving(self.cfg) as (host, port):
            req = b"GET /f.txt HTTP/1.1\r\nHost: x\r\nrAnGe: bytes=0-3\r\nConnection: close\r\n\r\n"
            resp = raw_exchange(host, port, req)
            self.assertEqual(status_of(resp), 206)

    def test_obs_fold_request_accepted(self):
        with serving(self.cfg) as (host, port):
            req = (
                b"GET /f.txt HTTP/1.1\r\nHost: x\r\nX-Folded: a\r\n  b\r\nConnection: close\r\n\r\n"
            )
            self.assertEqual(status_of(raw_exchange(host, port, req)), 200)

    def test_bad_version_400(self):
        # A malformed version errors before request_version is set, so the stdlib
        # emits it HTTP/0.9-style (no status line) — we match the base exactly.
        with serving(self.cfg) as (host, port):
            req = b"GET /f.txt HTTP/1.2.3\r\nHost: x\r\nConnection: close\r\n\r\n"
            resp = raw_exchange(host, port, req)
            self.assertNotIn(b"hi there", resp)  # the file is not served
            self.assertIn(b"400", resp)

    def test_http2_in_request_line_505(self):
        # A literal "HTTP/2.0" request line (not the h2 preface) is unsupported.
        with serving(self.cfg) as (host, port):
            req = b"GET /f.txt HTTP/2.0\r\nHost: x\r\nConnection: close\r\n\r\n"
            resp = raw_exchange(host, port, req)
            self.assertNotIn(b"hi there", resp)
            self.assertIn(b"505", resp)

    def test_http_0_9_request(self):
        # A 2-word "GET /path" line is HTTP/0.9: body only, no status line.
        with serving(self.cfg) as (host, port):
            resp = raw_exchange(host, port, b"GET /f.txt\r\n\r\n")
            self.assertIn(b"hi there", resp)


if __name__ == "__main__":
    unittest.main()
